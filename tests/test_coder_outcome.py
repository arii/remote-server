import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import coder
import deep_chat
import observe
from backend.shared.cli import repo_root

try:
    import deepagents  # noqa: F401
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

if HAS_DEPS:

    class LocalModel(FakeMessagesListChatModel):
        def bind_tools(self, *args, **kwargs):
            return self


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderOutcome(unittest.TestCase):
    def test_script_cli_can_write_a_file(self):
        repo = Path(repo_root())
        with tempfile.TemporaryDirectory() as root:
            Path(root, "sitecustomize.py").write_text(
                textwrap.dedent("""
                from pathlib import Path
                import deep_chat, observe
                from backend.shared import ledger, model_router
                from langchain_core.messages import AIMessage
                from tests.test_coder_outcome import LocalModel
                model = LocalModel(responses=[
                    AIMessage(content="", tool_calls=[{
                        "name": "write_file", "args": {"file_path": "result.txt", "content": "done"},
                        "id": "write", "type": "tool_call",
                    }]), AIMessage(content="DONE #1261"),
                ])
                model_router.resolve_llm = lambda: {"provider": "ollama", "model": "test"}
                model_router.build_chat_model = lambda **kwargs: (model, {"provider": "ollama", "model": "test"})
                deep_chat.open_checkpointer = lambda *args: (None, False, None)
                ledger.record = lambda *args, **kwargs: None
                observe.default_trace_path = lambda *args: str(Path.cwd() / "trace.jsonl")
            """)
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(repo / "deep-agent/coder.py"),
                    "--worktree",
                    root,
                    "Implement #1261",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        [root, str(repo), str(repo / "deep-agent")]
                    ),
                    "CODER_PLAN": "never",
                    "CODER_VERIFY_ROUNDS": "0",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DONE #1261", result.stdout)
            self.assertEqual(Path(root, "result.txt").read_text(), "done")

    def run_scenario(
        self,
        reply,
        operation=None,
        cli=False,
        rounds=0,
        setup=None,
        repair_files=(),
        verified=False,
        repair=False,
    ):
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")
            Path(root, "obsolete.txt").write_text("obsolete")
            if setup:
                setup(Path(root))

            calls = []
            actions = SimpleNamespace(
                delete=lambda path: calls.append(("delete", {"file_path": path})),
                write=lambda path, content: calls.append(
                    ("write_file", {"file_path": path, "content": content})
                ),
                edit=lambda path, old, new: calls.append(
                    (
                        "edit_file",
                        {"file_path": path, "old_string": old, "new_string": new},
                    )
                ),
            )
            if operation:
                operation(actions)
            responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": name, "args": args, "id": str(i), "type": "tool_call"}
                    ],
                )
                for i, (name, args) in enumerate(calls)
            ]
            model = LocalModel(responses=responses + [AIMessage(content=reply)])

            with (
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(model, {"provider": "ollama", "model": "test"}),
                ),
                mock.patch.object(coder, "verify_rounds", return_value=rounds),
                mock.patch.object(
                    coder, "trace_has_passing_check", return_value=verified
                ),
                mock.patch.object(
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(
                    observe, "default_trace_path", return_value=str(trace)
                ),
                mock.patch("backend.shared.ledger.record"),
            ):
                if cli:
                    result = coder._implement_and_finish(
                        root, "Implement #1261", "system", "thread", 5
                    )
                else:
                    try:
                        result = coder.run_coder(
                            root,
                            "Implement #1261",
                            "system",
                            "thread",
                            5,
                            repair_files=repair_files,
                            repair=repair,
                        )
                    except RuntimeError as exc:
                        result = exc
            return result, [json.loads(line) for line in trace.read_text().splitlines()]

    def test_full_coding_turn_fits_graph_budget_including_repairs(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                result, events = self.run_scenario(
                    "DONE #1261",
                    operation=lambda actions: [
                        actions.write(f"result-{index}.txt", "done")
                        for index in range(coder.REPAIR_MODEL_LIMIT - 1)
                    ],
                    repair=repair,
                )
                self.assertNotIsInstance(result, BaseException)
                self.assertEqual(result[0], "DONE #1261")
                self.assertTrue(events[-1]["ok"])

    def test_repair_model_call_limit_still_bounds_a_long_coding_turn(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        with self.assertRaises(ModelCallLimitExceededError):
            self.run_scenario(
                "DONE #1261",
                operation=lambda actions: [
                    actions.write(f"result-{index}.txt", "done")
                    for index in range(coder.REPAIR_MODEL_LIMIT)
                ],
                repair=True,
            )

    def test_initial_turn_can_exceed_repair_model_budget(self):
        result, events = self.run_scenario(
            "DONE #1261",
            operation=lambda actions: [
                actions.write(f"result-{index}.txt", "done")
                for index in range(coder.REPAIR_MODEL_LIMIT + 2)
            ],
        )
        self.assertNotIsInstance(result, BaseException)
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_initial_model_budget_is_bounded(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        self.assertGreater(coder.INITIAL_MODEL_LIMIT, coder.REPAIR_MODEL_LIMIT)
        with self.assertRaises(ModelCallLimitExceededError):
            self.run_scenario(
                "DONE #1261",
                operation=lambda actions: [
                    actions.write(f"result-{index}.txt", "done")
                    for index in range(coder.INITIAL_MODEL_LIMIT)
                ],
            )

    def test_flatline_is_failed_with_missing_contract_reason(self):
        result, events = self.run_scenario("")
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("DONE", str(result))
        self.assertIn("no written files", str(result))
        self.assertFalse(events[-1]["ok"])
        self.assertIn("DONE", events[-2]["reason"])

    def test_done_without_writes_fails(self):
        result, events = self.run_scenario("DONE #1261")
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(events[-1]["ok"])

    def test_verified_repair_counts_existing_scoped_diff(self):
        result, events = self.run_scenario(
            "DONE #1261",
            setup=self.existing_diff,
            repair_files=("a.py",),
            verified=True,
            rounds=1,
        )
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_existing_diff_does_not_bypass_empty_or_unverified_turns(self):
        for files, verified in (((), True), (("other.py",), True), (("a.py",), False)):
            with self.subTest(files=files, verified=verified):
                result, _ = self.run_scenario(
                    "DONE #1261",
                    setup=self.existing_diff,
                    repair_files=files,
                    verified=verified,
                )
                self.assertIsInstance(result, RuntimeError)
                self.assertIn("no written files", str(result))

    def test_repair_diff_read_error_fails_closed(self):
        result, _ = self.run_scenario(
            "DONE #1261",
            repair_files=("a.py",),
            verified=True,
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("cannot inspect", str(result))

    def test_repair_with_reverted_diff_still_fails(self):
        def setup(root):
            self.existing_diff(root)
            (root / "a.py").write_text("before\n")

        result, _ = self.run_scenario(
            "DONE #1261",
            setup=setup,
            repair_files=("a.py",),
            verified=True,
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("no written files", str(result))

    def test_existing_diff_covers_staged_new_and_deleted_scoped_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.existing_diff(root)
            subprocess.run(["git", "-C", directory, "add", "a.py"], check=True)
            (root / "new.py").write_text("new\n")
            (root / "outside.py").write_text("unrelated\n")
            self.assertEqual(
                coder.existing_implementation_files(directory, ("a.py", "new.py")),
                {"a.py", "new.py"},
            )
            (root / "a.py").unlink()
            self.assertEqual(
                coder.existing_implementation_files(directory, ("a.py",)), {"a.py"}
            )
            with self.assertRaises(ValueError):
                coder.existing_implementation_files(directory, ("../outside.py",))

    @staticmethod
    def existing_diff(root):
        def git(*args):
            subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True
            )

        git("init", "-q")
        Path(root, "a.py").write_text("before\n")
        git("add", "a.py")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        )
        Path(root, "a.py").write_text("after\n")

    def test_deletion_only_implementation_succeeds(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.delete("obsolete.txt")
        )
        self.assertNotIsInstance(result, RuntimeError)
        self.assertTrue(events[-1]["ok"])

    def test_failed_delete_does_not_count(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.delete("missing.txt")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(events[-1]["ok"])

    def test_write_without_terminal_contract_fails(self):
        result, events = self.run_scenario(
            "Implemented it", lambda b: b.write("a.txt", "hello")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("DONE", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_done_after_write_succeeds(self):
        result, events = self.run_scenario(
            "Changed a.txt\nDONE #1261", lambda b: b.write("a.txt", "hello")
        )
        self.assertEqual(result[0], "Changed a.txt\nDONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_failed_edit_does_not_count_as_a_write(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.edit("missing.txt", "a", "b")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("no written files", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_unverified_preserves_terminal_contract(self):
        result, _ = self.run_scenario(
            "DONE #1261", lambda b: b.write("a.txt", "hello"), rounds=1
        )
        self.assertEqual(result[0].splitlines()[-1], "DONE #1261")

    def test_successful_edit_counts(self):
        def operation(actions):
            actions.write("a.txt", "before")
            actions.edit("a.txt", "before", "after")

        result, events = self.run_scenario("DONE #1261", operation)
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])
        self.assertTrue(any(event.get("name") == "edit_file" for event in events))

    def test_blocked_is_not_success(self):
        result, events = self.run_scenario("BLOCKED #1261")
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("BLOCKED", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_contract_must_be_terminal(self):
        result, _ = self.run_scenario("DONE #1261\nStill working")
        self.assertIsInstance(result, RuntimeError)

    def test_cli_flatline_returns_nonzero(self):
        result, events = self.run_scenario("", cli=True)
        self.assertNotEqual(result, 0)
        self.assertFalse(events[-1]["ok"])
