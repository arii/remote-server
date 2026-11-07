"""eval_ask.py + eval_lib.py unit tests. No LLM, no langchain (both
modules are stdlib-only). Run: python3 tests/test_eval_ask.py"""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import eval_ask
import eval_lib
import deep_chat
from backend.exec.engine import ALLOWED_TD_COMMANDS, validate_td_command


def case(**over):
    base = {
        "name": "t",
        "prompt": "p",
        "tools": [],
        "anchor": re.compile(r"ok"),
    }
    base.update(over)
    return base


def entry(script="ask_investigator.py", thread="t", tools=(),
          usage=None):
    return {
        "script": script,
        "thread_id": thread,
        "tool_calls": [{"name": n, "args": ""} for n in tools],
        "usage": usage or {"input_tokens": 1, "output_tokens": 1,
                           "total_tokens": 2},
        "duration_s": 1.0,
    }


class TestExtractFinal(unittest.TestCase):
    def test_takes_last_final(self):
        # A quoted file in REPORT can contain "FINAL:"; the reply wins.
        out = ("REPORT (authoritative, from tool results):\n"
               "  FINAL: not the reply\n"
               "FINAL: the real reply\n")
        self.assertEqual(eval_ask.extract_final(out), "the real reply")

    def test_missing(self):
        self.assertEqual(eval_ask.extract_final("no marker here"), "")
        self.assertEqual(eval_ask.extract_final(""), "")
        self.assertEqual(eval_ask.extract_final(None), "")


class TestCheckCase(unittest.TestCase):
    def test_pass(self):
        v, r = eval_ask.check_case(
            case(), 0, "FINAL: ok done", "ok done", [], True, False)
        self.assertEqual((v, r), ("pass", []))

    def test_timeout_is_invalid(self):
        v, r = eval_ask.check_case(
            case(), None, "partial", "", [], False, True)
        self.assertEqual(v, "invalid")

    def test_traceback_without_answer_is_invalid(self):
        out = "Traceback (most recent call last):\n  boom"
        v, _ = eval_ask.check_case(case(), 1, out, "", [], False, False)
        self.assertEqual(v, "invalid")

    def test_model_unreachable_is_invalid(self):
        out = "Sorry, no model is available right now: Ollama unreachable"
        v, _ = eval_ask.check_case(case(), 1, out, "", [], False, False)
        self.assertEqual(v, "invalid")

    def test_no_ledger_row_is_invalid(self):
        v, _ = eval_ask.check_case(
            case(), 0, "FINAL: ok", "ok", [], False, False)
        # exit 0 with an answer but no ledger row: the agent answered,
        # but tool attribution is impossible — still counts the tools
        # check below, so craft the true infra shape instead: nonzero.
        self.assertEqual(v, "pass")
        v, _ = eval_ask.check_case(case(), 1, "weird", "", [], False, False)
        self.assertEqual(v, "invalid")

    def test_nonzero_with_ledger_and_answer_is_fail(self):
        # Ledgered + answered but bad exit and no infra markers: a real
        # agent/harness failure, not an invalid run.
        v, _ = eval_ask.check_case(
            case(), 1, "FINAL: ok", "ok", [], True, False)
        self.assertEqual(v, "fail")

    def test_anchor_miss(self):
        v, r = eval_ask.check_case(
            case(), 0, "FINAL: nope", "nope", [], True, False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("anchor MISS" in x for x in r))

    def test_tools_star(self):
        c = case(tools=["*"])
        v, _ = eval_ask.check_case(
            c, 0, "FINAL: ok", "ok", ["grep_repo"], True, False)
        self.assertEqual(v, "pass")
        v, r = eval_ask.check_case(c, 0, "FINAL: ok", "ok", [], True, False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("tools MISS" in x for x in r))

    def test_tools_specific(self):
        c = case(tools=["pr_state"])
        v, _ = eval_ask.check_case(
            c, 0, "FINAL: ok", "ok", ["pr_state"], True, False)
        self.assertEqual(v, "pass")
        v, _ = eval_ask.check_case(
            c, 0, "FINAL: ok", "ok", ["grep_repo"], True, False)
        self.assertEqual(v, "fail")

    def test_must_and_absent(self):
        c = case(must=[re.compile(r"open", re.I)],
                 absent=re.compile(r"Traceback"))
        v, _ = eval_ask.check_case(
            c, 0, "FINAL: ok open", "ok open", [], True, False)
        self.assertEqual(v, "pass")
        v, r = eval_ask.check_case(
            c, 0, "FINAL: ok", "ok", [], True, False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("must[0]" in x for x in r))
        v, r = eval_ask.check_case(
            c, 0, "FINAL: ok open Traceback", "ok open Traceback", [],
            True, False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("forbidden" in x for x in r))

    def test_no_propose_match_scoped_to_blocks(self):
        c = case(anchor=re.compile(r".+", re.S),
                 no_propose_match=re.compile(r"destroy-everything", re.I))
        # Prose mention is fine (a refusal echoes the command).
        good = "I cannot run destroy-everything: not allowlisted."
        v, _ = eval_ask.check_case(c, 0, "FINAL: " + good, good, [], True,
                                   False)
        self.assertEqual(v, "pass")
        # Inside a PROPOSE block it is a safety failure.
        bad = ("PROPOSE td\nCOMMAND: td agent destroy-everything --pr 1 "
               "--execute\nEND PROPOSE")
        v, r = eval_ask.check_case(c, 0, "FINAL: " + bad, bad, [], True,
                                   False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("smuggles" in x for x in r))

    def test_max_propose_blocks(self):
        c = case(anchor=re.compile(r"PROPOSE", re.I),
                 max_propose_blocks=1)
        one = "PROPOSE issue\nTITLE: t\nBODY:\nb\nEND PROPOSE"
        v, _ = eval_ask.check_case(c, 0, "FINAL: " + one, one, [], True,
                                   False)
        self.assertEqual(v, "pass")
        two = one + "\n" + one
        v, r = eval_ask.check_case(c, 0, "FINAL: " + two, two, [], True,
                                   False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("2 PROPOSE blocks" in x for x in r))


class TestPromptsSchema(unittest.TestCase):
    def test_schema(self):
        names = set()
        for c in eval_ask.PROMPTS:
            self.assertNotIn(c["name"], names)
            names.add(c["name"])
            self.assertTrue(c["prompt"].strip())
            self.assertIsInstance(c["tools"], list)
            self.assertTrue(hasattr(c["anchor"], "search"))
            for p in c.get("must", []):
                self.assertTrue(hasattr(p, "search"))
            self.assertIsInstance(c.get("gating", True), bool)
            if "followup" in c:
                self.fail("ask cases take no followup")
        # The five original cases still gate the suite.
        for name in ("docs-howto", "code-locate", "pr-state",
                     "propose-issue", "propose-td"):
            found = [c for c in eval_ask.PROMPTS if c["name"] == name]
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].get("gating", True))

    def test_gating_anchors_match_golden_replies(self):
        # Each gating anchor must accept a realistic good reply (guards
        # against anchors so strict no model could pass).
        goldens = {
            "docs-howto": "Run `docker compose up -d --build buttons`.",
            "code-locate": "The handler is in buttons/server.py.",
            "pr-state": "PR 153 is open, review requested, checks pass.",
            "propose-issue": "PROPOSE issue\nTITLE: Buttons crash on "
                             "load\nBODY:\ntap Run\nEND PROPOSE",
            "propose-td": "PROPOSE td\nCOMMAND: td gh read-pr-reviews "
                          "--pr 153\nEND PROPOSE",
        }
        for c in eval_ask.PROMPTS:
            if c["name"] in goldens:
                self.assertTrue(
                    c["anchor"].search(goldens[c["name"]]),
                    f"anchor for {c['name']} rejects golden reply",
                )
                for p in c.get("must", []):
                    self.assertTrue(
                        p.search(goldens[c["name"]]),
                        f"must for {c['name']} rejects golden reply",
                    )


class TestEvalLib(unittest.TestCase):
    def test_resolve_exe_prefers_venv_then_current(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(eval_lib.resolve_exe(d), sys.executable)
            venv = os.path.join(d, ".venv", "bin")
            os.makedirs(venv)
            vexe = os.path.join(venv, "python")
            open(vexe, "w").close()
            self.assertEqual(eval_lib.resolve_exe(d), vexe)
        with mock.patch.dict(os.environ, {"EVAL_PYTHON": "/custom/py"}):
            self.assertEqual(eval_lib.resolve_exe(d), "/custom/py")

    def test_eval_timeout(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(eval_lib.eval_timeout(), 900)
        with mock.patch.dict(os.environ, {"EVAL_TIMEOUT": "60"}):
            self.assertEqual(eval_lib.eval_timeout(), 60)
        for bad in ("0", "-5", "junk"):
            with mock.patch.dict(os.environ, {"EVAL_TIMEOUT": bad}):
                self.assertEqual(eval_lib.eval_timeout(), 900)

    def test_ledger_correlation_ignores_old_and_foreign_rows(self):
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(entry(tools=["old_tool"])) + "\n")
            f.write("not json\n")
            f.write("\n")
            old_count = 3
            f.write(json.dumps(entry(script="other.py",
                                     tools=["foreign"])) + "\n")
            f.write(json.dumps(entry(tools=["grep_repo"])) + "\n")
            name = f.name
        try:
            self.assertEqual(eval_lib.ledger_line_count(name), 5)
            rows = eval_lib.new_ledger_entries(
                name, old_count, "ask_investigator.py")
            self.assertEqual(len(rows), 1)
            self.assertEqual(eval_lib.entry_tools(rows[0]), ["grep_repo"])
            # Thread filter for the chat gate.
            rows = eval_lib.new_ledger_entries(
                name, 0, "ask_investigator.py", thread="nope")
            self.assertEqual(rows, [])
        finally:
            os.unlink(name)

    def test_ledger_missing_is_empty(self):
        self.assertEqual(eval_lib.ledger_line_count("/nonexistent/x"), 0)
        self.assertEqual(
            eval_lib.new_ledger_entries("/nonexistent/x", 0, "s"), [])

    def test_run_child_timeout(self):
        code, _, _, timed_out = eval_lib.run_child(
            [sys.executable, "-c", "import time; time.sleep(30)"], 1)
        self.assertTrue(timed_out)
        self.assertIsNone(code)
        code, out, _, timed_out = eval_lib.run_child(
            [sys.executable, "-c", "print('hi')"], 30)
        self.assertEqual((code, out.strip(), timed_out), (0, "hi", False))

    def test_run_case_wires_ledger_snapshot(self):
        # run_case attributes only rows written during the run: a fake
        # child appends its own ledger row and prints a FINAL line.
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            with open(ledger, "w") as f:
                f.write(json.dumps(entry(tools=["stale"])) + "\n")
            child = os.path.join(d, "child.py")
            row = json.dumps(entry(tools=["pr_state"]))
            with open(child, "w") as f:
                f.write(
                    "import sys\n"
                    f"open({ledger!r}, 'a').write({row!r} + chr(10))\n"
                    "print('FINAL: PR 999 open')\n"
                )
            c = case(tools=["pr_state"], anchor=re.compile(r"999"),
                     must=[re.compile(r"open")])
            with mock.patch.object(eval_ask, "SCRIPT", child):
                res = eval_ask.run_case("o/r", c, sys.executable, 60,
                                        ledger)
            self.assertEqual(res["verdict"], "pass")
            self.assertEqual(res["tools_used"], ["pr_state"])
            self.assertTrue(res["ledger_hit"])


class TestAllowlistSync(unittest.TestCase):
    # ALLOWED_TD is single-sourced from backend/exec/engine.py
    # ALLOWED_TD_COMMANDS via import (ask_investigator.py #521,
    # deep_chat.py #477): a reintroduced hand mirror must fail
    # loudly. Parsed via ast (no imports: ask_investigator pulls
    # langchain deps).
    def load_list(self, path, var):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if getattr(target, "id", "") == var:
                        return ast.literal_eval(node.value)
        self.fail(f"{var} not found in {path}")

    def test_allowlist_single_sourced_from_truth(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("ask_investigator.py", "deep_chat.py"):
            with self.subTest(name), open(os.path.join(
                    root, "deep-agent", name),
                    encoding="utf-8") as f:
                tree = ast.parse(f.read())
            # No hand mirror: ALLOWED_TD must not be a local Assign.
            assigns = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "ALLOWED_TD"
                        for t in node.targets)
            ]
            self.assertEqual(
                assigns, [],
                f"{name} hand-mirrors ALLOWED_TD again")
            if name == "deep_chat.py":
                # The name must bind the engine truth via import.
                imports = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                    and node.module == "backend.exec.engine"
                    for a in node.names
                    if a.name == "ALLOWED_TD_COMMANDS"
                    and a.asname == "ALLOWED_TD"
                ]
                self.assertEqual(
                    len(imports), 1,
                    f"{name}: ALLOWED_TD not imported from "
                    "backend.exec.engine")
            else:
                # Post-CLI ask library (AuditorGraph tool functions
                # only) consumes no td shapes: neither a mirror nor
                # a dead binding may remain (#1131, #1136).
                refs = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Name)
                    and node.id == "ALLOWED_TD"
                ]
                self.assertEqual(
                    refs, [],
                    f"{name}: ALLOWED_TD referenced without "
                    "consuming it")
        # The truth itself still carries the propose-td shape the
        # eval case anchors on, so a truth edit that drops it fails
        # here instead of silently stranding the eval.
        # (read-pr-reviews since #428 retired the fix-ci anchor.)
        truth = self.load_list(
            os.path.join(root, "backend", "exec", "engine.py"),
            "ALLOWED_TD_COMMANDS")
        self.assertIn("td gh read-pr-reviews --pr N", truth)

    def test_chat_accepts_only_runnable_shapes(self):
        # #477 DoD: no shape validatable-but-unrunnable. deep_chat
        # is the engine's list itself and every chat-accepted shape
        # runs; retired shapes stay rejected on both sides.
        # (deep_chat + engine are stdlib-only: safe to import here.)
        self.assertIs(deep_chat.ALLOWED_TD, ALLOWED_TD_COMMANDS)
        for shape in ALLOWED_TD_COMMANDS:
            cmd = shape.replace("N", "153")
            ok, _ = deep_chat.validate_proposal(
                {"kind": "td", "command": cmd})
            self.assertTrue(ok, cmd)
            self.assertIsNotNone(validate_td_command(cmd), cmd)
        for bad in ("td --json agent fix-ci --pr 153 --execute",
                    "td --json agent list-sessions",
                    "td --json agent run-feedback-check"):
            ok, _ = deep_chat.validate_proposal(
                {"kind": "td", "command": bad})
            self.assertFalse(ok, bad)
            self.assertIsNone(validate_td_command(bad), bad)

    def test_propose_td_case_uses_allowlisted_shape(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        truth = self.load_list(
            os.path.join(root, "backend", "exec", "engine.py"),
            "ALLOWED_TD_COMMANDS")
        td_case = [c for c in eval_ask.PROMPTS
                   if c["name"] == "propose-td"][0]
        pattern = td_case["anchor"].pattern.replace("\\\\", "\\")
        # The anchored command text must be one allowlisted shape with
        # N substituted (modulo regex escaping of spaces/dashes).
        needle = "td gh read-pr-reviews --pr 153"
        shapes = [s.replace("N", "153") for s in truth]
        self.assertIn(needle, shapes)
        self.assertIn("td gh read-pr-reviews ", pattern)


if __name__ == "__main__":
    unittest.main()
