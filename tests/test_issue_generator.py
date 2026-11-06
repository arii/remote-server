"""Tests for deep-agent/issue_generator.py (stdlib only, no model).

Graph integration cases need langgraph (deep-agent venv) and skip
otherwise. Run agent cases with: deep-agent/.venv/bin/python
tests/test_issue_generator.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import issue_generator

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _valid_td(args):
    return 0, json.dumps({"status": "success", "valid": True,
                          "allowed_files": [], "forbidden_files": [],
                          "verification_commands": ["make check"],
                          "acceptance_criteria": ["done"], "errors": []})


class TestEvidenceCitesRepoFile(unittest.TestCase):
    def test_hits_existing_file_with_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "widget.py")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            self.assertEqual(
                issue_generator.evidence_cites_repo_file(
                    "see widget.py:3 for the bug", tmp),
                ["widget.py"])

    def test_misses_have_no_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                issue_generator.evidence_cites_repo_file(
                    "researched the Kubernetes Python client at length",
                    tmp),
                [])

    def test_nonexistent_and_absolute_paths_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                issue_generator.evidence_cites_repo_file(
                    "see missing.py:1 and /etc/hosts and ../up.py", tmp),
                [])

    def test_ollama_url_default_is_compose_host(self):
        old = os.environ.get("OLLAMA_URL")
        os.environ.pop("OLLAMA_URL", None)
        try:
            self.assertEqual(issue_generator._ollama_base_url(),
                             "http://ollama:11434")
        finally:
            if old is not None:
                os.environ["OLLAMA_URL"] = old

    def test_backend_probe_skipped_for_injected_model(self):
        gen = issue_generator.IssueGeneratorGraph(
            repo_root=tempfile.gettempdir(),
            model_fn=lambda: None)
        ok, _ = gen.check_backend()
        self.assertTrue(ok)

    def test_backend_probe_fails_closed_on_dead_port(self):
        old = os.environ.get("OLLAMA_URL")
        os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"
        try:
            gen = issue_generator.IssueGeneratorGraph(
                repo_root=tempfile.gettempdir())
            ok, message = gen.check_backend(timeout=2)
            self.assertFalse(ok)
            self.assertIn("OLLAMA_URL", message)
        finally:
            if old is None:
                os.environ.pop("OLLAMA_URL", None)
            else:
                os.environ["OLLAMA_URL"] = old

    def test_backend_probe_passes_on_live_port(self):
        import socket
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        old = os.environ.get("OLLAMA_URL")
        os.environ["OLLAMA_URL"] = f"http://127.0.0.1:{port}"
        try:
            gen = issue_generator.IssueGeneratorGraph(
                repo_root=tempfile.gettempdir())
            ok, _ = gen.check_backend(timeout=2)
            self.assertTrue(ok)
        finally:
            server.close()
            if old is None:
                os.environ.pop("OLLAMA_URL", None)
            else:
                os.environ["OLLAMA_URL"] = old


class TestDraftPrompt(unittest.TestCase):
    def test_draft_demands_read_first(self):
        seen = []

        def model_fn():
            class FakeModel:
                def invoke(self, prompt):
                    seen.append(prompt)
                    return "draft"
            return FakeModel()

        gen = issue_generator.IssueGeneratorGraph(
            repo_root=tempfile.gettempdir(), model_fn=model_fn)
        gen.draft_text("Fix it", "ctx", [], {})
        self.assertTrue(seen)
        self.assertIn("read one repo file", seen[0].lower())
        self.assertIn("quote", seen[0].lower())


class TestAssembleSpec(unittest.TestCase):
    def test_required_sections_present(self):
        spec = issue_generator.assemble_spec("Fix the widget", "ctx",
                                             {"Q?": "A."})
        for section in ("Problem Statement", "Goal", "Scope",
                        "Definition of Done", "Scope & File Boundaries",
                        "Implementation Plan", "Acceptance Criteria",
                        "Verification Commands"):
            self.assertIn(section, spec)
        self.assertIn("Q?", spec)

    def test_extract_questions(self):
        text = ("Some prose.\n- What files are in scope?\n"
                "1. Who owns this?\n- Not a question\n")
        self.assertEqual(
            issue_generator.extract_questions(text),
            ["What files are in scope?", "Who owns this?"])
        self.assertEqual(issue_generator.extract_questions("none"), [])


class TestValidateSpec(unittest.TestCase):
    def _gen(self, td_runner):
        return issue_generator.IssueGeneratorGraph(
            repo_root=tempfile.gettempdir(), td_runner=td_runner)

    def test_valid(self):
        gen = self._gen(_valid_td)
        valid, data, errors = gen.validate_spec("# T\n\nbody\n")
        self.assertTrue(valid)
        self.assertEqual(errors, [])
        self.assertEqual(data["verification_commands"], ["make check"])

    def test_invalid_loops_with_errors(self):
        def runner(args):
            return 1, json.dumps({"status": "success", "valid": False,
                                  "allowed_files": [], "forbidden_files": [],
                                  "verification_commands": [],
                                  "acceptance_criteria": [],
                                  "errors": ["missing section: Goal"]})
        gen = self._gen(runner)
        valid, _data, errors = gen.validate_spec("# T\n\nbody\n")
        self.assertFalse(valid)
        self.assertIn("missing section: Goal", errors)

    def test_missing_td_is_an_error(self):
        def runner(args):
            raise FileNotFoundError("no td")
        gen = self._gen(runner)
        valid, _data, errors = gen.validate_spec("x")
        self.assertFalse(valid)
        self.assertTrue(errors)

    def test_td_init_reports_status(self):
        gen = self._gen(lambda args: (0, json.dumps({"status": "ready"})))
        ok, payload = gen.td_init()
        self.assertTrue(ok)
        self.assertEqual(payload["status"], "ready")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestGraphLoop(unittest.TestCase):
    def _gen(self, tmp):
        with open(os.path.join(tmp, "widget.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("x = 1\n")

        def model_fn():
            class FakeModel:
                def invoke(self, prompt):
                    return ("Draft spec body.\n\n"
                            "- What files are in scope?\n")
            return FakeModel()

        def investigate_fn(brief, draft):
            return "evidence: widget.py"

        def td_runner(args):
            if args and args[0] == "init":
                return 0, json.dumps({"status": "ready"})
            return _valid_td(args)

        return issue_generator.IssueGeneratorGraph(
            repo_root=tmp, model_fn=model_fn,
            investigate_fn=investigate_fn, td_runner=td_runner,
            max_loops=3, thread_id="test-thread-1")

    def test_interrupt_resume_validates_and_persists(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp)
                state = gen.run("Fix the widget", context="ctx")
                self.assertIn("__interrupt__", state)
                questions = state["__interrupt__"][0].value["questions"]
                self.assertIn("What files are in scope?", questions)
                final = gen.run(
                    "Fix the widget",
                    resume={"What files are in scope?": "widget.py"})
                self.assertTrue(final.get("valid"))
                self.assertTrue(final.get("persisted"))
            finally:
                ledger.LEDGER = old
            with open(ledger_path, encoding="utf-8") as handle:
                entry = json.loads(handle.read().strip().splitlines()[-1])
            self.assertEqual(entry.get("script"), "issue_generator.py")

    def test_invalid_spec_loops_to_draft(self):
        calls = []

        def td_runner(args):
            if args and args[0] == "init":
                return 0, json.dumps({"status": "ready"})
            calls.append(1)
            if len(calls) == 1:
                return 1, json.dumps(
                    {"status": "success", "valid": False,
                     "allowed_files": [], "forbidden_files": [],
                     "verification_commands": [],
                     "acceptance_criteria": [],
                     "errors": ["missing section: Goal"]})
            return _valid_td(args)

        def model_fn():
            class FakeModel:
                def invoke(self, prompt):
                    return "Draft spec body, no questions."
            return FakeModel()

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ev.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("x = 1\n")
            gen = issue_generator.IssueGeneratorGraph(
                repo_root=tmp, model_fn=model_fn,
                investigate_fn=lambda b, d: "evidence: ev.py",
                td_runner=td_runner, max_loops=3,
                thread_id="test-thread-2")
            final = gen.run("Fix the widget")
            self.assertTrue(final.get("valid"))
            self.assertGreaterEqual(final.get("attempts", 0), 2)

    def test_exhausted_loops_fail_closed(self):
        def td_runner(args):
            if args and args[0] == "init":
                return 0, json.dumps({"status": "ready"})
            return 1, json.dumps(
                {"status": "success", "valid": False, "allowed_files": [],
                 "forbidden_files": [], "verification_commands": [],
                 "acceptance_criteria": [],
                 "errors": ["always invalid"]})

        def model_fn():
            class FakeModel:
                def invoke(self, prompt):
                    return "Draft, no questions."
            return FakeModel()

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "ev.py"), "w",
                      encoding="utf-8") as handle:
                handle.write("x = 1\n")
            gen = issue_generator.IssueGeneratorGraph(
                repo_root=tmp, model_fn=model_fn,
                investigate_fn=lambda b, d: "evidence: ev.py",
                td_runner=td_runner, max_loops=2,
                thread_id="test-thread-3")
            final = gen.run("Fix the widget")
            self.assertFalse(final.get("valid"))
            self.assertIn("always invalid", final.get("errors", []))

    def test_off_brief_evidence_fails_loud_without_looping(self):
        def td_runner(args):
            if args and args[0] == "init":
                return 0, json.dumps({"status": "ready"})
            return _valid_td(args)

        def model_fn():
            class FakeModel:
                def invoke(self, prompt):
                    return "Draft spec body, no questions."
            return FakeModel()

        with tempfile.TemporaryDirectory() as tmp:
            gen = issue_generator.IssueGeneratorGraph(
                repo_root=tmp, model_fn=model_fn,
                investigate_fn=lambda b, d: (
                    "researched the Kubernetes Python client at length"),
                td_runner=td_runner, max_loops=5,
                thread_id="test-thread-4")
            final = gen.run("Fix the widget")
            self.assertFalse(final.get("valid"))
            self.assertTrue(final.get("off_brief"))
            self.assertTrue(any("off-brief" in e
                               for e in final.get("errors", [])))
            self.assertLessEqual(final.get("attempts", 0), 1)


if __name__ == "__main__":
    unittest.main()
