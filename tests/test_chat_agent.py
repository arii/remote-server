"""chat_agent argv/history contract tests. Needs the deep-agent venv
(langchain import at module top). No LLM calls: every case exits
before model resolution. Run: deep-agent/.venv/bin/python
tests/test_chat_agent.py"""
import base64
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import chat_agent


def b64(text):
    return base64.b64encode(text.encode()).decode()


class TestDecodeHistory(unittest.TestCase):
    def test_round_trip(self):
        raw = b64('[{"role": "user", "content": "hi"},'
                  ' {"role": "assistant", "content": "yo"}]')
        msgs = chat_agent._decode_history(raw)
        self.assertEqual([m.content for m in msgs], ["hi", "yo"])

    def test_rejects(self):
        self.assertIsNone(chat_agent._decode_history("!!!"))
        self.assertIsNone(chat_agent._decode_history(b64('{"a": 1}')))
        self.assertIsNone(chat_agent._decode_history(b64('[1, 2]')))
        self.assertIsNone(
            chat_agent._decode_history(b64('[{"role": "x", "content": "y"}]')))
        self.assertIsNone(
            chat_agent._decode_history(b64('[{"role": "user"}]')))
        big = b64("[" + ",".join(
            ['{"role": "user", "content": "x"}'] * 21) + "]")
        self.assertIsNone(chat_agent._decode_history(big))


class TestArgvValidation(unittest.TestCase):
    SCRIPT = os.path.join(
        os.path.dirname(__file__), "..", "deep-agent", "chat_agent.py")

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_bad_repo(self):
        p = self.run_script("nope", "s1", b64("hi"))
        self.assertEqual(p.returncode, 2)

    def test_bad_session(self):
        p = self.run_script("arii/remote-server", "bad id!", b64("hi"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("session-id", p.stderr)

    def test_bad_prompt(self):
        p = self.run_script("arii/remote-server", "s1", "!!!")
        self.assertEqual(p.returncode, 2)

    def test_empty_prompt(self):
        p = self.run_script("arii/remote-server", "s1", b64("   "))
        self.assertEqual(p.returncode, 2)

    def test_bad_history(self):
        p = self.run_script("arii/remote-server", "s1", b64("hi"), "!!!")
        self.assertEqual(p.returncode, 2)
        self.assertIn("history", p.stderr)


class TestSystemPrompt(unittest.TestCase):
    def test_how_to_reads_docs_first(self):
        # Regression pin for the turn-docs early-stop guess: the agent
        # must read docs/README/compose before answering how-to, never
        # guess run commands from directory names.
        self.assertIn("read the relevant docs, README, or ",
                      chat_agent.SYSTEM)
        self.assertIn("never guess run commands from ",
                      chat_agent.SYSTEM)


if __name__ == "__main__":
    unittest.main()
