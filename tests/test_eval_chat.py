"""eval_chat.py unit tests + chat-slice contract tests. No LLM, no
langchain (eval_chat is stdlib-only; chat_agent.py is inspected via ast
so these run on system python). Run: python3 tests/test_eval_chat.py"""
import ast
import base64
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import eval_chat

ROOT = os.path.join(os.path.dirname(__file__), "..")
CHAT_AGENT = os.path.join(ROOT, "deep-agent", "chat_agent.py")


def case(**over):
    base = {
        "name": "t",
        "prompt": "p",
        "tools": [],
        "anchor": re.compile(r"ok"),
    }
    base.update(over)
    return base


def entry(tools=(), usage=None, duration=1.0):
    return {
        "script": "chat_agent.py",
        "thread_id": "eval-t",
        "tool_calls": [{"name": n, "args": ""} for n in tools],
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "total_tokens": 2} if usage is None else usage,
        "duration_s": duration,
    }


class TestBuildHistoryB64(unittest.TestCase):
    def test_round_trip(self):
        raw = eval_chat.build_history_b64([("hi", "yo"), ("q2", "a2")])
        items = json.loads(base64.b64decode(raw).decode())
        self.assertEqual(
            items,
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "yo"},
             {"role": "user", "content": "q2"},
             {"role": "assistant", "content": "a2"}],
        )

    def test_caps_at_contract_limit(self):
        raw = eval_chat.build_history_b64([("u", "x" * 5000)])
        items = json.loads(base64.b64decode(raw).decode())
        self.assertEqual(len(items[1]["content"]), 2000)


class TestCheckChatCase(unittest.TestCase):
    def test_pass(self):
        v, r = eval_chat.check_chat_case(
            case(), 0, "ok", "ok done", [], [entry()], False)
        self.assertEqual((v, r), ("pass", []))

    def test_timeout_and_model_failure_are_invalid(self):
        v, _ = eval_chat.check_chat_case(
            case(), None, "", "", [], [], True)
        self.assertEqual(v, "invalid")
        out = "Sorry, no model is available right now: Nebius needs key"
        v, _ = eval_chat.check_chat_case(case(), 1, out, "", [], [], False)
        self.assertEqual(v, "invalid")

    def test_rogue_tool_is_safety_fail(self):
        c = case(tools=["*"])
        v, r = eval_chat.check_chat_case(
            c, 0, "ok", "ok", ["grep_repo", "shell_exec"], [entry()], False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("SAFETY" in x for x in r))

    def test_readonly_tools_pass(self):
        c = case(tools=["*"])
        v, _ = eval_chat.check_chat_case(
            c, 0, "ok", "ok", ["list_dir", "grep_repo", "read_repo_file"],
            [entry()], False)
        self.assertEqual(v, "pass")

    def test_require_usage(self):
        c = case(require_usage=True)
        v, _ = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [entry()], False)
        self.assertEqual(v, "pass")
        v, r = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [], False)
        # Answered but unattributed: usage accounting genuinely failed.
        self.assertEqual(v, "fail")
        self.assertTrue(any("usage MISS" in x for x in r))
        v, r = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [entry(usage={"x": 1})], False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("usage MISS" in x for x in r))
        v, _ = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [entry(duration=-1)], False)
        self.assertEqual(v, "fail")

    def test_usage_rejects_bool_counts(self):
        # True is an int instance; token counts must be real ints.
        v, _ = eval_chat.check_chat_case(
            case(require_usage=True), 0, "ok", "ok", [],
            [entry(usage={"input_tokens": True, "output_tokens": 0,
                          "total_tokens": 0})], False)
        self.assertEqual(v, "fail")

    def test_absent(self):
        c = case(anchor=re.compile(r".+", re.S),
                 absent=re.compile(r"deleted", re.I))
        v, _ = eval_chat.check_chat_case(
            c, 0, "out", "I cannot delete anything.", [], [entry()],
            False)
        self.assertEqual(v, "pass")
        v, _ = eval_chat.check_chat_case(
            c, 0, "out", "deleted 3 dirs.", [], [entry()], False)
        self.assertEqual(v, "fail")


class TestRunChatCase(unittest.TestCase):
    def fake_turn(self, ledger, answer, tools=()):
        row = json.dumps(entry(tools=tools))
        child = os.path.join(os.path.dirname(ledger), "child.py")
        with open(child, "w") as f:
            f.write(
                "import sys\n"
                f"open({ledger!r}, 'a').write({row!r} + chr(10))\n"
                f"print({answer!r})\n"
            )
        return child

    def test_wrong_thread_rows_do_not_count(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            child = self.fake_turn(ledger, "run docker compose up",
                                   ["grep_repo"])
            c = case(name="turn-docs", tools=["*"],
                     anchor=re.compile(r"compose"))
            with mock.patch.object(eval_chat, "SCRIPT", child):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
            # The fake row used thread eval-t, not eval-turn-docs, so
            # the anchor passes but the tools check fails: thread
            # correlation ignores foreign rows.
            self.assertEqual(res["verdict"], "fail")
            self.assertEqual(res["turns"], 1)
            self.assertFalse(res["ledger_hit"])
            self.assertTrue(any("tools MISS" in r
                                for r in res["reasons"]))

    def test_thread_correlation(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            row = json.dumps(entry(tools=["grep_repo"]))
            row = row.replace("eval-t", "eval-turn-x")
            child = os.path.join(d, "child.py")
            with open(child, "w") as f:
                f.write(
                    "import sys\n"
                    f"open({ledger!r}, 'a').write({row!r} + chr(10))\n"
                    "print('see buttons/server.py')\n"
                )
            c = case(name="turn-x", tools=["grep_repo"],
                     anchor=re.compile(r"buttons/server\.py"))
            with mock.patch.object(eval_chat, "SCRIPT", child):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
            self.assertEqual(res["verdict"], "pass")
            self.assertEqual(res["tools_used"], ["grep_repo"])

    def test_followup_threads_history(self):
        seen = {}

        def turn(repo, session, prompt, exe, timeout, history_b64=None):
            seen.setdefault("calls", []).append(
                (prompt, history_b64))
            if history_b64 is None:
                return 0, "noted the buttons server", "", False, 0.1
            return 0, "you mentioned buttons", "", False, 0.1

        c = case(name="resume", anchor=re.compile(r".+", re.S),
                 followup={"prompt": "Which service?",
                           "anchor": re.compile(r"buttons")})
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            with mock.patch.object(eval_chat, "run_turn",
                                    side_effect=turn):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["turns"], 2)
        self.assertEqual(len(seen["calls"]), 2)
        items = json.loads(base64.b64decode(seen["calls"][1][1]).decode())
        self.assertEqual(items[0]["role"], "user")
        self.assertIn("buttons server", items[1]["content"])

    def test_followup_skipped_when_turn1_fails(self):
        def turn(repo, session, prompt, exe, timeout, history_b64=None):
            return 1, "", "boom", False, 0.1

        c = case(name="resume", anchor=re.compile(r".+", re.S),
                 followup={"prompt": "Which service?",
                           "anchor": re.compile(r"buttons")})
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            with mock.patch.object(eval_chat, "run_turn",
                                    side_effect=turn):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
        self.assertEqual(res["verdict"], "invalid")
        self.assertEqual(res["turns"], 2)  # shape recorded, turn 2 skipped


class TestChatPromptsSchema(unittest.TestCase):
    def test_schema(self):
        names = set()
        for c in eval_chat.PROMPTS:
            self.assertNotIn(c["name"], names)
            names.add(c["name"])
            self.assertTrue(c["prompt"].strip())
            self.assertIsInstance(c["tools"], list)
            self.assertTrue(hasattr(c["anchor"], "search"))
            self.assertIsInstance(c.get("gating", True), bool)
            # Session ids derive from names; must fit SESSION_RE.
            session = "eval-" + c["name"]
            self.assertTrue(
                re.match(r"^[A-Za-z0-9_-]{1,64}$", session))
            if "followup" in c:
                self.assertTrue(c["followup"]["prompt"].strip())
                self.assertTrue(hasattr(
                    c["followup"]["anchor"], "search"))
        kinds = {c["name"] for c in eval_chat.PROMPTS}
        for needed in ("turn-docs", "turn-tool-use", "usage-accounting",
                       "turn-refuse-write", "resume"):
            self.assertIn(needed, kinds)

    def test_golden_replies(self):
        goldens = {
            "turn-docs": "Run `docker compose up -d buttons`.",
            "turn-tool-use": "The handler is in buttons/server.py.",
            "usage-accounting": "ledger check ok",
            "turn-refuse-write": "I cannot delete anything: read-only.",
            "resume": "noted.",
        }
        for c in eval_chat.PROMPTS:
            golden = goldens[c["name"]]
            if c["name"] == "resume":
                golden = "you mentioned buttons"
                anchor = c["followup"]["anchor"]
            else:
                anchor = c["anchor"]
            self.assertTrue(anchor.search(golden), c["name"])
            absent = c.get("absent")
            if absent is not None:
                self.assertIsNone(absent.search(golden), c["name"])


class TestChatAgentContract(unittest.TestCase):
    # chat_agent.py needs the venv (langchain); inspect the source via
    # ast instead so this runs on system python.

    def load(self):
        with open(CHAT_AGENT, encoding="utf-8") as f:
            return f.read()

    def test_tools_are_readonly_only(self):
        tree = ast.parse(self.load())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if getattr(dec, "id", "") == "tool":
                        names.add(node.name)
        self.assertTrue(names, "no @tool functions found")
        self.assertEqual(names, eval_chat.READONLY_TOOLS)

    def test_system_prompt_refuses_writes(self):
        src = self.load()
        self.assertRegex(src, r"cannot run, change, or deploy")
        self.assertRegex(src, r"read-only", re.I)

    def test_history_contract_constants(self):
        tree = ast.parse(self.load())
        consts = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if getattr(target, "id", "") in (
                            "MAX_HISTORY", "SESSION_RE"):
                        consts[target.id] = ast.dump(node.value)
        self.assertIn("MAX_HISTORY", consts)
        self.assertIn("20", consts["MAX_HISTORY"])
        self.assertIn("SESSION_RE", consts)

    def test_stdout_is_pure_answer(self):
        # Every print() without file=sys.stderr must be answer text or
        # the trailing newline: progress goes to stderr so the server's
        # SSE chunk reader sees only the reply.
        tree = ast.parse(self.load())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "print"):
                continue
            keys = [k.arg for k in node.keywords]
            if "file" in keys:
                continue
            src = ast.dump(node)
            self.assertTrue(
                "chunk" in src or "reply" in src
                or "Sorry" in src or "flush" in src,
                f"non-answer print to stdout: {src[:200]}",
            )

    def test_ledger_records_usage(self):
        self.assertIn("usage=usage", self.load())
        self.assertIn('"total_tokens"', self.load())


if __name__ == "__main__":
    unittest.main()
