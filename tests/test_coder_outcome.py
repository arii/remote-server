import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import coder
import deep_chat
import observe

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
    def run_scenario(self, reply, operation=None, cli=False, rounds=0):
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")

            calls = []
            actions = SimpleNamespace(
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
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(
                    observe, "default_trace_path", return_value=str(trace)
                ),
                mock.patch("ledger.record"),
            ):
                if cli:
                    result = coder._implement_and_finish(
                        root, "Implement #1261", "system", "thread", 5
                    )
                else:
                    try:
                        result = coder.run_coder(
                            root, "Implement #1261", "system", "thread", 5
                        )
                    except RuntimeError as exc:
                        result = exc
            return result, [json.loads(line) for line in trace.read_text().splitlines()]

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
