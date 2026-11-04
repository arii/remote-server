"""eval_chat.py unit tests + live-chat contract tests. No LLM, no
langchain (eval_chat and deep_chat are stdlib-only at import; the
@tool surface is inspected via ast). Run: python3
tests/test_eval_chat.py"""
import ast
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import eval_chat
import deep_chat

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEEP_CHAT = os.path.join(ROOT, "deep-agent", "deep_chat.py")


def case(**over):
    base = {
        "name": "t",
        "prompt": "p",
        "tools": [],
        "anchor": re.compile(r"ok"),
    }
    base.update(over)
    return base


def icase(**over):
    base = {
        "name": "t",
        "prompt": "p",
        "tools": ["propose_action"],
        "anchor": re.compile(r"INTERRUPT:"),
        "expect_interrupt": True,
        "approve": {
            "verdict": "approve",
            "anchor": re.compile(r"PROPOSE issue"),
        },
    }
    base.update(over)
    return base


def entry(tools=(), usage=None, duration=1.0, mode="turn",
          thread="eval-t"):
    return {
        "script": "deep_chat.py",
        "thread_id": thread,
        "tool_calls": [{"name": n, "args": ""} for n in tools],
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "total_tokens": 2} if usage is None else usage,
        "duration_s": duration,
        "extra": {"mode": mode,
                  "decisions": [{"what": "w", "why": "y"}]},
    }


def blob(thread="eval-t", proposal=True):
    value = {"action_requests": [{"args": {"kind": "issue"}}]} \
        if proposal else {"note": "nothing validatable"}
    return {"thread_id": thread,
            "interrupts": [{"id": "i1", "value": value}]}


def resume(code=0, answer="PROPOSE issue", out=None, timed_out=False):
    return {"code": code, "out": answer if out is None else out,
            "answer": answer, "timed_out": timed_out}


class TestSessionFor(unittest.TestCase):
    def test_fits_thread_contract(self):
        for name in ("turn-docs", "propose-approve", "resume-thread"):
            self.assertTrue(
                deep_chat.valid_thread_id(eval_chat.session_for(name)),
                name)

    def test_unique_per_call(self):
        seen = {eval_chat.session_for("t") for _ in range(20)}
        self.assertEqual(len(seen), 20)

    def test_derives_from_case_name(self):
        self.assertIn("turn-docs",
                      eval_chat.session_for("turn-docs"))


class TestExtractInterrupt(unittest.TestCase):
    def test_last_line_wins(self):
        stdout = ('INTERRUPT: {"thread_id": "old", "interrupts": []}\n'
                  'INTERRUPT: {"thread_id": "new", "interrupts": []}\n')
        self.assertEqual(
            eval_chat.extract_interrupt(stdout)["thread_id"], "new")

    def test_missing_is_none(self):
        self.assertIsNone(eval_chat.extract_interrupt("just text\n"))

    def test_bad_json_is_none(self):
        self.assertIsNone(eval_chat.extract_interrupt("INTERRUPT: {\n"))

    def test_non_dict_is_none(self):
        self.assertIsNone(eval_chat.extract_interrupt("INTERRUPT: [1]\n"))


class TestStripMarkers(unittest.TestCase):
    def test_drops_interrupt_lines(self):
        self.assertEqual(
            eval_chat.strip_markers('text\nINTERRUPT: {"a": 1}\nmore'),
            "text\nmore")

    def test_plain_text_untouched(self):
        self.assertEqual(eval_chat.strip_markers("  hi  "), "hi")


class TestInterruptCarriesProposal(unittest.TestCase):
    def test_action_requests(self):
        self.assertTrue(eval_chat.interrupt_carries_proposal(
            blob(proposal=True)))

    def test_bare_kind(self):
        payload = {"thread_id": "t",
                   "interrupts": [{"id": "i",
                                   "value": {"kind": "td"}}]}
        self.assertTrue(eval_chat.interrupt_carries_proposal(payload))

    def test_empty_or_missing(self):
        self.assertFalse(eval_chat.interrupt_carries_proposal(
            blob(proposal=False)))
        self.assertFalse(eval_chat.interrupt_carries_proposal(
            {"thread_id": "t", "interrupts": []}))
        self.assertFalse(eval_chat.interrupt_carries_proposal({}))
        self.assertFalse(eval_chat.interrupt_carries_proposal(None))


class TestCheckLedgerRows(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            eval_chat.check_ledger_rows([entry()], 1), [])

    def test_missing_rows(self):
        reasons = eval_chat.check_ledger_rows([], 1)
        self.assertTrue(any("ledger MISS" in r for r in reasons))

    def test_shape_mode_and_decisions(self):
        bad_mode = entry()
        bad_mode["extra"] = {"mode": "bogus",
                             "decisions": [{"what": "w", "why": "y"}]}
        reasons = eval_chat.check_ledger_rows([bad_mode], 1)
        self.assertTrue(any("mode" in r for r in reasons))
        bad_dec = entry()
        bad_dec["extra"] = {"mode": "turn", "decisions": []}
        reasons = eval_chat.check_ledger_rows([bad_dec], 1)
        self.assertTrue(any("decisions" in r for r in reasons))
        missing_extra = entry()
        del missing_extra["extra"]
        reasons = eval_chat.check_ledger_rows([missing_extra], 1)
        self.assertTrue(any("SHAPE" in r for r in reasons))

    def test_resume_mode_accepted(self):
        self.assertEqual(
            eval_chat.check_ledger_rows([entry(mode="resume")], 1), [])

    def test_budget_cap_per_turn(self):
        cap = deep_chat.CHAT_TOOL_BUDGET + eval_chat.BUDGET_SLACK
        heavy = entry(tools=["grep_repo"] * (cap + 1))
        reasons = eval_chat.check_ledger_rows([heavy], 1)
        self.assertTrue(any("BUDGET" in r for r in reasons))
        # Same call count spread over two turns fits the cap.
        reasons = eval_chat.check_ledger_rows([heavy], 2)
        self.assertFalse(any("BUDGET" in r for r in reasons))

    def test_require_usage(self):
        self.assertEqual(
            eval_chat.check_ledger_rows([entry()], 1, True), [])
        reasons = eval_chat.check_ledger_rows(
            [entry(usage={"x": 1})], 1, True)
        self.assertTrue(any("usage MISS" in r for r in reasons))


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

    def test_unexpected_interrupt_is_fail_not_invalid(self):
        v, r = eval_chat.check_chat_case(
            case(), 3, "INTERRUPT: {}", "", [], [entry()], False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("INTERRUPT" in x for x in r))

    def test_rogue_tool_is_safety_fail(self):
        c = case(tools=["*"])
        for rogue in ("shell_exec", "task", "write_file", "edit_file"):
            v, r = eval_chat.check_chat_case(
                c, 0, "ok", "ok", ["grep_repo", rogue],
                [entry()], False)
            self.assertEqual(v, "fail", rogue)
            self.assertTrue(any("SAFETY" in x for x in r), rogue)

    def test_chat_and_harness_tools_pass(self):
        c = case(tools=["*"])
        v, _ = eval_chat.check_chat_case(
            c, 0, "ok", "ok",
            ["list_dir", "grep_repo", "read_repo_file", "search_docs",
             "propose_action", "run_repo_command", "fetch_url",
             "search_turns", "write_todos", "ls", "read_file",
             "glob", "grep"],
            [entry()], False)
        self.assertEqual(v, "pass")

    def test_answered_without_ledger_is_fail(self):
        v, r = eval_chat.check_chat_case(
            case(), 0, "ok", "ok done", [], [], False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("ledger MISS" in x for x in r))

    def test_require_usage(self):
        c = case(require_usage=True)
        v, _ = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [entry()], False)
        self.assertEqual(v, "pass")
        v, r = eval_chat.check_chat_case(
            c, 0, "ok", "ok", [], [entry(usage={"x": 1})], False)
        self.assertEqual(v, "fail")
        self.assertTrue(any("usage MISS" in x for x in r))

    def test_usage_rejects_bool_counts(self):
        # True is an int instance; token counts must be real ints.
        v, _ = eval_chat.check_chat_case(
            case(require_usage=True), 0, "ok", "ok", [],
            [entry(usage={"input_tokens": True, "output_tokens": 0,
                          "total_tokens": 0})], False)
        self.assertEqual(v, "fail")

    def test_absent(self):
        c = case(anchor=re.compile(r".+", re.S),
                 absent=re.compile(r"INTERRUPT:"))
        v, _ = eval_chat.check_chat_case(
            c, 0, "out", "plain answer.", [], [entry()], False)
        self.assertEqual(v, "pass")
        v, _ = eval_chat.check_chat_case(
            c, 0, "out", "INTERRUPT: {}", [], [entry()], False)
        self.assertEqual(v, "fail")


class TestCheckInterruptCase(unittest.TestCase):
    def check(self, c=None, code=3, stdout=None, used=("propose_action",),
            entries=None, timed_out=False, thread="eval-t",
            interrupt="default", resume_result="default", turns=1):
        c = icase() if c is None else c
        stdout = ('INTERRUPT: {"thread_id": "eval-t", "interrupts": []}'
                  if stdout is None else stdout)
        default_entries = entries is None
        entries = [entry(thread=thread)] \
            if entries is None else entries
        if interrupt == "default":
            interrupt = blob(thread)
        if resume_result == "default":
            resume_result = resume()
            if default_entries:
                entries = entries + [entry(mode="resume",
                                           thread=thread)]
        return eval_chat.check_interrupt_case(
            c, code, stdout, stdout, list(used), entries, timed_out,
            thread, interrupt, resume_result, turns)

    def test_pass(self):
        v, r = self.check()
        self.assertEqual((v, r), ("pass", []))

    def test_timeout_is_invalid(self):
        v, _ = self.check(timed_out=True)
        self.assertEqual(v, "invalid")

    def test_answered_instead_of_proposing_is_fail(self):
        v, r = self.check(code=0, stdout="here is the fix",
                        interrupt=None, resume_result=None)
        self.assertEqual(v, "fail")
        self.assertTrue(any("answered instead" in x for x in r))

    def test_exit1_without_ledger_is_invalid(self):
        v, _ = self.check(code=1, stdout="boom", used=[], entries=[],
                        interrupt=None, resume_result=None)
        self.assertEqual(v, "invalid")

    def test_exit1_sorry_is_invalid(self):
        v, r = self.check(code=1, stdout="Sorry, the assistant failed: x",
                        used=[], interrupt=None, resume_result=None)
        # Sorry marker: infra failure, not an agent failure.
        self.assertEqual(v, "invalid", r)

    def test_missing_blob_is_fail(self):
        v, r = self.check(stdout="no blob here", interrupt=None,
                        resume_result=None,
                        c=icase(anchor=re.compile(r".+", re.S)))
        self.assertEqual(v, "fail")
        self.assertTrue(any("parseable" in x for x in r))

    def test_thread_mismatch_is_fail(self):
        v, r = self.check(interrupt=blob("other-thread"),
                        resume_result=None)
        self.assertEqual(v, "fail")
        self.assertTrue(any("mismatch" in x for x in r))

    def test_blob_without_proposal_is_fail(self):
        v, r = self.check(interrupt=blob(proposal=False),
                        resume_result=None)
        self.assertEqual(v, "fail")
        self.assertTrue(any("no proposal" in x for x in r))

    def test_missing_propose_tool_is_miss(self):
        v, r = self.check(used=["grep_repo"])
        self.assertEqual(v, "fail")
        self.assertTrue(any("tools MISS" in x for x in r))

    def test_approve_step_skipped_is_fail(self):
        v, r = self.check(resume_result=None)
        self.assertEqual(v, "fail")
        self.assertTrue(any("never ran" in x for x in r))

    def test_resume_anchor_miss_is_fail(self):
        v, r = self.check(resume_result=resume(answer="something else"))
        self.assertEqual(v, "fail")
        self.assertTrue(any("anchor MISS" in x for x in r))

    def test_resume_timeout_is_invalid(self):
        v, _ = self.check(resume_result=resume(timed_out=True))
        self.assertEqual(v, "invalid")

    def test_resume_still_pending_is_fail(self):
        v, r = self.check(resume_result=resume(
            code=3, answer="", out="INTERRUPT: {}"))
        self.assertEqual(v, "fail")
        self.assertTrue(any("still pending" in x for x in r))

    def test_resume_model_failure_is_invalid(self):
        v, _ = self.check(resume_result=resume(
            code=1, answer="",
            out="Sorry, no model is available right now: down"))
        self.assertEqual(v, "invalid")

    def test_missing_resume_row_is_fail(self):
        v, r = self.check(entries=[entry(thread="eval-t")])
        self.assertEqual(v, "fail")
        self.assertTrue(any("no resume row" in x for x in r))

    def test_missing_turn_row_is_fail(self):
        v, r = self.check(entries=[entry(mode="resume",
                                       thread="eval-t")])
        self.assertEqual(v, "fail")
        self.assertTrue(any("no turn row" in x for x in r))

    def test_no_approve_spec_checks_turn_only(self):
        c = icase()
        del c["approve"]
        v, r = self.check(c=c, resume_result=None,
                        entries=[entry(thread="eval-t")])
        self.assertEqual((v, r), ("pass", []))


class TestRunChatCase(unittest.TestCase):
    def fake_child(self, ledger, thread, answer, tools=()):
        row = json.dumps(entry(tools=tools, thread=thread))
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
            child = self.fake_child(ledger, "eval-t",
                                    "run docker compose up", ["grep_repo"])
            c = case(name="turn-docs", tools=["*"],
                     anchor=re.compile(r"compose"))
            with mock.patch.object(eval_chat, "SCRIPT", child), \
                    mock.patch.object(eval_chat, "session_for",
                                      return_value="eval-turn-docs"):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
            # The fake row used thread eval-t, not eval-turn-docs, so
            # tools + ledger checks fail: thread correlation ignores
            # foreign rows.
            self.assertEqual(res["verdict"], "fail")
            self.assertEqual(res["turns"], 1)
            self.assertEqual(res["thread"], "eval-turn-docs")
            self.assertFalse(res["ledger_hit"])
            self.assertTrue(any("tools MISS" in r
                                for r in res["reasons"]))

    def test_thread_correlation(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            child = self.fake_child(ledger, "eval-turn-x",
                                    "see buttons/server.py", ["grep_repo"])
            c = case(name="turn-x", tools=["grep_repo"],
                     anchor=re.compile(r"buttons/server\.py"))
            with mock.patch.object(eval_chat, "SCRIPT", child), \
                    mock.patch.object(eval_chat, "session_for",
                                      return_value="eval-turn-x"):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
            self.assertEqual(res["verdict"], "pass")
            self.assertEqual(res["tools_used"], ["grep_repo"])

    def test_followup_reuses_thread_without_history_argv(self):
        seen = {}

        def turn(repo, session, prompt, exe, timeout):
            seen.setdefault("calls", []).append((session, prompt))
            if len(seen["calls"]) == 1:
                return 0, "noted the buttons server", "", False, 0.1
            return 0, "you mentioned buttons", "", False, 0.1

        c = case(name="resume-thread", anchor=re.compile(r".+", re.S),
                 followup={"prompt": "Which service?",
                           "anchor": re.compile(r"buttons")})
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            with open(ledger, "w") as f:
                f.write(json.dumps(entry(
                    thread="eval-resume-thread-1-1-1")) + "\n")
            # Rows predate the run, so the ledger check misses — but
            # the threading shape is what this test pins: same
            # thread, no history payload, followup anchor wins.
            with mock.patch.object(eval_chat, "run_turn",
                                    side_effect=turn), \
                    mock.patch.object(
                        eval_chat, "session_for",
                        return_value="eval-resume-thread-1-1-1"):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
        self.assertEqual(res["turns"], 2)
        self.assertEqual(len(seen["calls"]), 2)
        self.assertEqual(seen["calls"][0][0], seen["calls"][1][0])
        self.assertIn("Which service?", seen["calls"][1][1])

    def test_followup_second_turn_writes_ledger_rows(self):
        def turn(repo, session, prompt, exe, timeout):
            with open(ledger, "a") as f:
                f.write(json.dumps(entry(
                    tools=["grep_repo"], thread=session)) + "\n")
            if "Which service?" in prompt:
                return 0, "you mentioned buttons", "", False, 0.1
            return 0, "noted the buttons server", "", False, 0.1

        c = case(name="resume-thread", anchor=re.compile(r".+", re.S),
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
        self.assertEqual(res["tools_used"], ["grep_repo"])

    def test_followup_skipped_when_turn1_fails(self):
        def turn(repo, session, prompt, exe, timeout):
            return 1, "", "boom", False, 0.1

        c = case(name="resume-thread", anchor=re.compile(r".+", re.S),
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
        self.assertEqual(res["turns"], 1)

    def test_interrupt_approve_flow(self):
        def turn(repo, session, prompt, exe, timeout):
            with open(ledger, "a") as f:
                f.write(json.dumps(entry(
                    tools=["propose_action"], thread=session)) + "\n")
            payload = json.dumps(blob(session))
            return 3, f"INTERRUPT: {payload}", "", False, 0.1

        def do_resume(repo, session, verdict, exe, timeout,
                      message=None):
            assert verdict == "approve"
            with open(ledger, "a") as f:
                f.write(json.dumps(entry(
                    mode="resume", thread=session)) + "\n")
            return 0, ("PROPOSE issue\nTITLE: T\nBODY:\nB\n"
                       "END PROPOSE"), "", False, 0.1

        c = icase(name="propose-approve")
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            with mock.patch.object(eval_chat, "run_turn",
                                    side_effect=turn), \
                    mock.patch.object(eval_chat, "run_resume",
                                      side_effect=do_resume):
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["turns"], 2)
        self.assertTrue(res["interrupt"])
        self.assertEqual(res["resume_exit"], 0)
        self.assertIn("propose_action", res["tools_used"])

    def test_interrupt_resume_skipped_without_blob(self):
        def turn(repo, session, prompt, exe, timeout):
            return 3, "exit 3 but no blob", "", False, 0.1

        c = icase(name="propose-approve")
        with tempfile.TemporaryDirectory() as d:
            ledger = os.path.join(d, "ledger.jsonl")
            open(ledger, "w").close()
            with mock.patch.object(eval_chat, "run_turn",
                                    side_effect=turn), \
                    mock.patch.object(eval_chat, "run_resume") as rm:
                res = eval_chat.run_chat_case("o/r", c, sys.executable,
                                              60, ledger)
        rm.assert_not_called()
        self.assertEqual(res["verdict"], "fail")
        self.assertIsNone(res["resume_exit"])


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
            # Threads derive from names; session ids must fit the
            # live thread contract.
            self.assertTrue(deep_chat.valid_thread_id(
                eval_chat.session_for(c["name"])))
            self.assertFalse("followup" in c
                             and "expect_interrupt" in c,
                             c["name"])
            if "followup" in c:
                self.assertTrue(c["followup"]["prompt"].strip())
                self.assertTrue(hasattr(
                    c["followup"]["anchor"], "search"))
            if "approve" in c:
                self.assertIn(c["approve"]["verdict"],
                              ("approve", "reject"))
                self.assertTrue(hasattr(
                    c["approve"]["anchor"], "search"))
        kinds = {c["name"] for c in eval_chat.PROMPTS}
        for needed in ("turn-docs", "turn-tool-use", "usage-accounting",
                       "propose-approve", "propose-reject",
                       "resume-thread"):
            self.assertIn(needed, kinds)

    def test_gating_set(self):
        gating = {c["name"] for c in eval_chat.PROMPTS
                  if c.get("gating", True)}
        self.assertEqual(gating, {"turn-docs", "turn-tool-use",
                                  "usage-accounting",
                                  "propose-approve"})

    def test_golden_replies(self):
        turn_goldens = {
            "turn-docs": "Run `docker compose up -d buttons`.",
            "turn-tool-use": "The handler is in buttons/server.py.",
            "usage-accounting": "ledger check ok",
            "propose-approve": 'INTERRUPT: {"thread_id": "t"}',
            "propose-reject": 'INTERRUPT: {"thread_id": "t"}',
            "resume-thread": "noted.",
        }
        for c in eval_chat.PROMPTS:
            golden = turn_goldens[c["name"]]
            if c["name"] == "resume-thread":
                golden = "you mentioned buttons"
                anchor = c["followup"]["anchor"]
            else:
                anchor = c["anchor"]
            self.assertTrue(anchor.search(golden), c["name"])
            absent = c.get("absent")
            if absent is not None:
                self.assertIsNone(absent.search(golden), c["name"])
            spec = c.get("approve")
            if spec is not None:
                if spec["verdict"] == "approve":
                    approval = ("PROPOSE issue\nTITLE: Buttons crash "
                                "on load\nBODY:\nTapping Run crashes "
                                "the page.\nEND PROPOSE")
                else:
                    approval = "Declined: I won't file the issue."
                self.assertTrue(spec["anchor"].search(approval),
                                c["name"])


class TestLiveTarget(unittest.TestCase):
    def test_script_is_deep_chat(self):
        self.assertEqual(eval_chat.SCRIPT_NAME, "deep_chat.py")
        self.assertTrue(eval_chat.SCRIPT.endswith("deep_chat.py"))
        self.assertNotIn("chat_agent", eval_chat.SCRIPT)

    def test_header_names_live_target(self):
        self.assertIn("deep_chat.py", eval_chat.__doc__)
        self.assertNotIn("does not exist yet", eval_chat.__doc__)
        self.assertNotIn("chat_agent", eval_chat.__doc__)

    def test_exit_constant_tracks_live(self):
        self.assertEqual(eval_chat.EXIT_NEEDS_APPROVAL,
                         deep_chat.EXIT_NEEDS_APPROVAL)


class TestDeepChatContract(unittest.TestCase):
    # deep_chat.py is stdlib-only at import; the @tool surface is
    # inspected via ast so this runs on system python.

    def load(self):
        with open(DEEP_CHAT, encoding="utf-8") as f:
            return f.read()

    def test_custom_tools_match_allowed_set(self):
        tree = ast.parse(self.load())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for dec in node.decorator_list:
                    if getattr(dec, "id", "") == "tool":
                        names.add(node.name)
        self.assertTrue(names, "no @tool functions found")
        self.assertEqual(names, eval_chat.DEEP_CHAT_TOOLS)
        self.assertTrue(names < eval_chat.ALLOWED_TOOLS)

    def test_harness_writes_are_not_allowed(self):
        for banned in ("task", "write_file", "edit_file"):
            self.assertNotIn(banned, eval_chat.ALLOWED_TOOLS)

    def test_system_prompt_proposes_writes(self):
        src = self.load()
        self.assertIn("write path is the propose_action tool", src)

    def test_interrupt_is_propose_only_approve_reject(self):
        self.assertEqual(set(deep_chat.INTERRUPT_ON),
                         {"propose_action"})
        cfg = deep_chat.INTERRUPT_ON["propose_action"]
        self.assertEqual(cfg["allowed_decisions"], ["approve", "reject"])
        self.assertEqual(deep_chat.EXIT_NEEDS_APPROVAL, 3)

    def test_resume_argv_exists(self):
        self.assertIn('"--resume"', self.load())
        self.assertIn("approve", self.load())

    def test_thread_contract(self):
        self.assertTrue(deep_chat.valid_thread_id("eval-t"))
        self.assertIn("THREAD_RE", self.load())

    def test_stdout_is_answer_or_machine_lines(self):
        # Every print() without file=sys.stderr must be answer text
        # (streamed chunks, salvage, reply), the INTERRUPT blob, the
        # post-approval PROPOSE texts, the --validate-proposal JSON,
        # or the trailing newline: progress goes to stderr so the
        # server's SSE chunk reader sees only the reply.
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
                "chunk" in src or "salvaged" in src
                or "INTERRUPT" in src or "json" in src
                or "reply" in src or "flush" in src
                or "text" in src,
                f"non-answer print to stdout: {src[:200]}",
            )

    def test_ledger_records_usage(self):
        self.assertIn("usage=", self.load())
        self.assertIn('"total_tokens"', self.load())

    def test_budget_constant_positive(self):
        self.assertGreater(deep_chat.CHAT_TOOL_BUDGET, 0)
        self.assertGreaterEqual(eval_chat.BUDGET_SLACK, 0)


if __name__ == "__main__":
    unittest.main()
