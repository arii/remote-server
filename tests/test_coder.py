"""Tests for coder.InitCoderGraph (stdlib only, no model).

Graph integration cases need langgraph (deep-agent venv) and skip
otherwise. Run agent cases with: deep-agent/.venv/bin/python
tests/test_coder.py
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import coder

try:
    import langgraph  # noqa: F401
    import langchain.agents.middleware  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _gate_ok(args, cwd):
    cmd = " ".join(args)
    if "verify-scope" in cmd:
        return 0, json.dumps(
            {
                "status": "success",
                "passed": True,
                "modified_files": [],
                "violations": [],
            }
        )
    if "audit-code" in cmd:
        return 0, json.dumps(
            {
                "status": "success",
                "passed": True,
                "slop_findings": [],
                "suppressions_added": [],
                "ast_violations": [],
            }
        )
    if "worktree" in cmd:
        return 0, json.dumps(
            {
                "status": "ready",
                "path": cwd,
                "branch": "agent/issue-1",
                "worktreeId": "issue-1",
            }
        )
    raise AssertionError(f"unexpected td call: {cmd}")


class TestWorktreeGuard(unittest.TestCase):
    def test_blocks_escape(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                coder.assert_inside(root, "../outside.txt")
            with self.assertRaises(ValueError):
                coder.assert_inside(root, "/etc/passwd")
            with self.assertRaises(ValueError):
                coder.assert_inside(root, "../../x")

    def test_allows_inside(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "sub", "f.py")
            os.makedirs(os.path.dirname(target))
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("x = 1\n")
            self.assertEqual(coder.assert_inside(root, "sub/f.py"), target)

    def test_gate_spec_markdown_shape(self):
        spec = coder.gate_spec_markdown("Fix it", ["a.py", "b.py"])
        for section in (
            "Problem Statement",
            "Goal",
            "Scope",
            "Definition of Done",
            "Scope & File Boundaries",
            "Acceptance Criteria",
        ):
            self.assertIn(section, spec)
        self.assertIn("`a.py`", spec)


class TestProvisionAndGate(unittest.TestCase):
    def test_provision_bad_payload(self):
        def td_runner(args, cwd):
            return 0, "not json"

        gen = coder.InitCoderGraph(
            repo="/tmp", issue_id=9, brief="x", td_runner=td_runner
        )
        root, error = gen.provision_worktree()
        self.assertIsNone(root)
        self.assertIn("non-JSON", error)

    def test_gate_collects_all_failures(self):
        def td_runner(args, cwd):
            cmd = " ".join(args)
            if "verify-scope" in cmd:
                return 1, json.dumps(
                    {
                        "status": "success",
                        "passed": False,
                        "modified_files": ["evil.py"],
                        "violations": ["evil.py"],
                    }
                )
            if "audit-code" in cmd:
                return 1, json.dumps(
                    {
                        "status": "success",
                        "passed": False,
                        "slop_findings": [],
                        "suppressions_added": [],
                        "ast_violations": [
                            {"file": "a.py", "line": 1, "rule": "r", "message": "m"}
                        ],
                    }
                )
            raise AssertionError(cmd)

        def cmd_runner(checkout, command):
            return "exit=1\nboom"

        gen = coder.InitCoderGraph(
            repo="/tmp",
            issue_id=9,
            brief="Fix it",
            td_runner=td_runner,
            cmd_runner=cmd_runner,
        )
        gate = gen.run_gate("/tmp", ["a.py"], ["make check"])
        self.assertFalse(gate["passed"])
        joined = "\n".join(gate["errors"])
        self.assertIn("scope violation: evil.py", joined)
        self.assertIn("audit: a.py:1: r: m", joined)
        self.assertIn("exit=1", joined)

    def test_gate_passes_clean(self):
        with tempfile.TemporaryDirectory() as root:
            gen = coder.InitCoderGraph(
                repo="/tmp",
                issue_id=9,
                brief="Fix it",
                td_runner=_gate_ok,
                cmd_runner=lambda c, cmd: "exit=0\nok",
            )
            gate = gen.run_gate(root, ["a.py"], ["make check"])
            self.assertTrue(gate["passed"])
            self.assertEqual(gate["errors"], [])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestCoderLoop(unittest.TestCase):
    def _gen(
        self,
        tmp,
        plan=None,
        implement="done",
        td_runner=None,
        cmds=None,
        thread="test-coder-1",
    ):
        def plan_fn(brief, root, context):
            return dict(
                plan or {"plan": "P", "allowed_files": [], "verification_commands": []}
            )

        def implement_fn(brief, plan_text, root, errors):
            return implement

        def cmd_runner(checkout, command):
            return (cmds or {}).get(command, "exit=0\nok")

        return coder.InitCoderGraph(
            repo=tmp,
            issue_id=7,
            brief="Fix it",
            plan_fn=plan_fn,
            implement_fn=implement_fn,
            td_runner=td_runner or _gate_ok,
            cmd_runner=cmd_runner,
            max_fix_loops=2,
            max_replans=1,
            thread_id=thread,
        )

    def test_approve_implement_gate_persist(self):
        from backend.shared import ledger

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
                entry = json.loads(handle.read().strip().splitlines()[-1])
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
                        {
                            "status": "success",
                            "passed": False,
                            "modified_files": [],
                            "violations": ["x.py"],
                        }
                    )
                return _gate_ok(args, cwd)
            if "audit-code" in cmd:
                return _gate_ok(args, cwd)
            if "worktree" in cmd:
                return 0, json.dumps(
                    {"status": "ready", "path": cwd, "branch": "b", "worktreeId": "w"}
                )
            raise AssertionError(f"unexpected td call: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner, thread="test-coder-2")
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
                return 1, json.dumps({"status": "error", "message": "always broken"})
            if "worktree" in cmd:
                return 0, json.dumps(
                    {"status": "ready", "path": cwd, "branch": "b", "worktreeId": "w"}
                )
            raise AssertionError(f"unexpected td call: {cmd}")

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner, thread="test-coder-3")
            gen.run()
            final = gen.run(resume={"decision": "approve"})
            self.assertFalse(final.get("valid"))

    def test_provision_failure_ends(self):
        def td_runner(args, cwd):
            raise RuntimeError("no td here")

        with tempfile.TemporaryDirectory() as tmp:
            gen = coder.InitCoderGraph(
                repo=tmp,
                issue_id=7,
                brief="x",
                td_runner=td_runner,
                plan_fn=lambda b, r, c: {"plan": "P"},
                implement_fn=lambda b, p, r, e: "done",
                thread_id="test-coder-4",
            )
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
            repo="/tmp",
            issue_id=7,
            brief="Fix it",
            td_runner=lambda args, cwd: (0, "{}"),
        )
        self.assertIs(gen.plan_fn, coder._default_plan_fn)
        self.assertIs(gen.implement_fn, coder._default_implement_fn)

    def test_explicit_fns_still_win(self):
        plan = lambda b, r, c: {"plan": "P"}  # noqa: E731
        gen = coder.InitCoderGraph(
            repo="/tmp",
            issue_id=7,
            brief="Fix it",
            plan_fn=plan,
            td_runner=lambda args, cwd: (0, "{}"),
        )
        self.assertIs(gen.plan_fn, plan)

    def test_default_plan_parses_files(self):
        fake = FakeModel("1. Add check [files: a.py, b.py]\n2. Wire it up")
        with mock.patch.object(coder, "build_model", return_value=(fake, {})):
            out = coder._default_plan_fn("Fix it", "/tmp/wt", {"errors": []})
        self.assertIn("Add check", out["plan"])
        self.assertEqual(sorted(out["allowed_files"]), ["a.py", "b.py"])
        self.assertEqual(out["verification_commands"], [])

    def test_default_implement_returns_notes(self):
        fake = FakeModel("implemented + verified")
        with mock.patch.object(coder, "build_model", return_value=(fake, {})):
            notes = coder._default_implement_fn("Fix it", "plan", "/tmp/wt", [])
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

    def test_run_with_verify_stops_on_stagnation(self):
        invocations = []

        def invoke():
            invocations.append(1)
            return "step"

        stagnant_flags = [False, True]

        def check_stagnant():
            return stagnant_flags.pop(0) if stagnant_flags else True

        result, rounds, verified = coder.run_with_verify(
            initial_invoke=invoke,
            reinvoke=invoke,
            has_passing_check=lambda: False,
            max_rounds=5,
            check_stagnant=check_stagnant,
        )
        # Round 0 is initial invoke, Round 1 reinvokes once, Round 2 sees stagnant=True and breaks
        self.assertFalse(verified)
        self.assertEqual(rounds, 1)
        self.assertEqual(len(invocations), 2)

    def test_recursion_limit_allows_bounded_model_and_tool_calls(self):
        self.assertGreaterEqual(
            coder.RECURSION_LIMIT,
            4 * (coder.REPAIR_MODEL_LIMIT + coder.REPAIR_TOOL_LIMIT),
        )
        self.assertLessEqual(coder.RECURSION_LIMIT, 150)

    @unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langchain_core)")
    def test_rules_name_only_tools_the_default_agent_has(self):
        # build_agent's default extra tool is run_check; the rules must
        # name it and must not advertise search_code (#1533), which no
        # agent receives.
        self.assertEqual(coder.build_check_tool("/tmp").name, "run_check")
        self.assertIn("run_check", coder.CODER_RULES)
        self.assertNotIn("search_code", coder.CODER_RULES)
        self.assertFalse(hasattr(coder, "build_search_tool"))


class TestReadCacheAndTruncation(unittest.TestCase):
    @unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langchain_core)")
    def test_empty_reads_are_bounded_but_failed_reads_are_not_cached(self):
        from langchain_core.messages import ToolMessage

        for status in ("success", "error"):
            with self.subTest(status=status):
                guard = coder.worktree_file_middleware("/tmp/wt", set()).wrap_tool_call
                request = mock.Mock(
                    tool_call={
                        "id": "read",
                        "name": "read_file",
                        "args": {"file_path": "a.py"},
                    }
                )
                request.override.side_effect = lambda **kwargs: types.SimpleNamespace(
                    **kwargs
                )
                handler = mock.Mock(
                    return_value=ToolMessage(
                        content="",
                        tool_call_id="read",
                        name="read_file",
                        status=status,
                    )
                )
                guard(request, handler)
                second = guard(request, handler)
                if status == "success":
                    self.assertIn("UNCHANGED", second.content)
                    with self.assertRaisesRegex(
                        coder.CoderBudgetExceeded, "BLOCKED.*unchanged read"
                    ):
                        guard(request, handler)
                else:
                    self.assertEqual(guard(request, handler).status, "error")

    def test_truncate_tool_output_short_and_exact(self):
        self.assertEqual(coder._truncate_tool_output("short", limit=4000), "short")
        exact = "x" * 100
        self.assertEqual(coder._truncate_tool_output(exact, limit=100), exact)

    def test_truncate_tool_output_long(self):
        head_mark = "HEADER_START_"
        tail_mark = "_TRAIL_END"
        body = head_mark + ("z" * 5000) + tail_mark
        truncated = coder._truncate_tool_output(body, limit=200)
        self.assertTrue(truncated.startswith(head_mark))
        self.assertTrue(truncated.endswith(tail_mark))
        self.assertIn("chars omitted", truncated)
        self.assertLess(len(truncated), 300)

    def test_read_cache_duplicate_detection(self):
        cache = coder.ReadCache()
        is_dup, d1 = cache.check_and_update("src/main.py", "def main(): pass")
        self.assertFalse(is_dup)
        self.assertEqual(len(d1), 64)

        is_dup2, d2 = cache.check_and_update("src/main.py", "def main(): pass")
        self.assertTrue(is_dup2)
        self.assertEqual(d1, d2)

    def test_read_cache_invalidation(self):
        cache = coder.ReadCache()
        cache.check_and_update("src/main.py", "def main(): pass")
        self.assertIsNotNone(cache.get("src/main.py"))

        cache.invalidate("src/main.py")
        self.assertIsNone(cache.get("src/main.py"))

        is_dup, _ = cache.check_and_update("src/main.py", "def main(): pass")
        self.assertFalse(is_dup)

    def test_read_cache_content_mutation(self):
        cache = coder.ReadCache()
        _, d1 = cache.check_and_update("src/main.py", "v1")
        is_dup, d2 = cache.check_and_update("src/main.py", "v2")
        self.assertFalse(is_dup)
        self.assertNotEqual(d1, d2)

    @unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langchain_core)")
    def test_worktree_file_middleware_cache_and_truncation(self):
        from langchain_core.messages import ToolMessage

        written = set()
        cache = coder.ReadCache()
        middleware = coder.worktree_file_middleware(
            "/tmp/wt", written, read_cache=cache
        )
        call_mw = getattr(middleware, "wrap_tool_call", middleware)

        # Mock tool call request object
        class FakeRequest:
            def __init__(self, tool_call):
                self.tool_call = tool_call

            def override(self, **kwargs):
                if "tool_call" in kwargs:
                    self.tool_call = kwargs["tool_call"]
                return self

        # 1. First read
        req1 = FakeRequest(
            {"id": "call_1", "name": "read_file", "args": {"file_path": "a.py"}}
        )
        res1 = call_mw(
            req1,
            lambda r: ToolMessage(
                content="print('hello')",
                tool_call_id="call_1",
                name="read_file",
                status="success",
            ),
        )
        self.assertEqual(res1.content, "print('hello')")

        # 2. Duplicate read -> returns sentinel
        req2 = FakeRequest(
            {"id": "call_2", "name": "read_file", "args": {"file_path": "a.py"}}
        )
        res2 = call_mw(
            req2,
            lambda r: ToolMessage(
                content="print('hello')",
                tool_call_id="call_2",
                name="read_file",
                status="success",
            ),
        )
        self.assertIn("[UNCHANGED: a.py", res2.content)
        self.assertIn("already in context. Do not re-read.]", res2.content)

        with self.assertRaisesRegex(
            coder.CoderBudgetExceeded, "BLOCKED.*unchanged read"
        ):
            call_mw(
                req2,
                lambda r: ToolMessage(
                    content="print('hello')",
                    tool_call_id="call_2",
                    name="read_file",
                    status="success",
                ),
            )

        # 3. Edit file -> invalidates cache
        req_edit = FakeRequest(
            {"id": "call_3", "name": "edit_file", "args": {"file_path": "a.py"}}
        )
        call_mw(
            req_edit,
            lambda r: ToolMessage(
                content="ok", tool_call_id="call_3", name="edit_file", status="success"
            ),
        )
        self.assertIn("a.py", written)

        # 4. Third read after edit -> returns fresh content
        req4 = FakeRequest(
            {"id": "call_4", "name": "read_file", "args": {"file_path": "a.py"}}
        )
        res4 = call_mw(
            req4,
            lambda r: ToolMessage(
                content="print('updated')",
                tool_call_id="call_4",
                name="read_file",
                status="success",
            ),
        )
        self.assertEqual(res4.content, "print('updated')")

        # 5. Long output truncation
        long_content = "X" * 15000
        req5 = FakeRequest(
            {"id": "call_5", "name": "read_file", "args": {"file_path": "big.py"}}
        )
        res5 = call_mw(
            req5,
            lambda r: ToolMessage(
                content=long_content,
                tool_call_id="call_5",
                name="read_file",
                status="success",
            ),
        )
        self.assertIn("chars omitted", res5.content)
        self.assertLess(len(res5.content), 10000)


def _install_fake_langchain(test):
    """Fake langchain_core.tools for stdlib-only build_check_tool tests."""

    lc = types.ModuleType("langchain_core")
    tools_mod = types.ModuleType("langchain_core.tools")

    def tool(fn):
        fn.description = fn.__doc__ or ""
        return fn

    tools_mod.tool = tool
    lc.tools = tools_mod
    saved = {}
    for name, mod in (
        ("langchain_core", lc),
        ("langchain_core.tools", tools_mod),
    ):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    def _restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    test.addCleanup(_restore)


def _check_entry(command, code, text):
    """A trace_check_history 4-tuple for a run_check with this output."""
    preview = "exit=%d\n%s" % (code, text)
    empty = code == 0 and not text.strip()
    digest = hashlib.sha1(preview.encode("utf-8")).hexdigest()
    return (command, code, empty, digest)


def _check_call_event(command, code, text):
    return {
        "type": "tool_call",
        "name": "run_check",
        "args_preview": command,
        "result_preview": "exit=%d\n%s" % (code, text),
        "ok": True,
    }


class TestUnchangedInspectionDither(unittest.TestCase):
    """#1374: repeated unchanged successful inspections count as dither."""

    CMD = "git diff --stat"

    def test_entry_unproductive_unchanged_output(self):
        first = _check_entry(self.CMD, 0, "1 file changed")
        repeat = _check_entry(self.CMD, 0, "1 file changed")
        changed = _check_entry(self.CMD, 0, "2 files changed")
        other = _check_entry("git status", 0, "1 file changed")
        self.assertFalse(coder._entry_unproductive(first))
        self.assertFalse(coder._entry_unproductive(repeat, changed))
        self.assertFalse(coder._entry_unproductive(repeat, other))
        self.assertTrue(coder._entry_unproductive(repeat, first))
        # A digest-less predecessor cannot prove sameness.
        self.assertFalse(coder._entry_unproductive(repeat, (self.CMD, 0, False)))
        # Legacy contracts preserved.
        self.assertTrue(coder._entry_unproductive((self.CMD, 1)))
        self.assertFalse(coder._entry_unproductive((self.CMD, 0)))
        self.assertTrue(coder._entry_unproductive((self.CMD, 0, True)))

    def test_repeated_unchanged_output_detected(self):
        history = [_check_entry(self.CMD, 0, "1 file changed")] * 6
        self.assertEqual(coder.find_dithered_command(history), self.CMD)

    def test_first_observation_is_productive(self):
        # The first unchanged-looking call is a legitimate inspection:
        # with no predecessor in history, the streak cannot be judged.
        history = [_check_entry(self.CMD, 0, "1 file changed")] * 5
        self.assertIsNone(coder.find_dithered_command(history))
        short = [_check_entry(self.CMD, 0, "1 file changed")] * 3
        self.assertIsNone(coder.find_dithered_command(short))

    def test_changed_output_breaks_streak(self):
        texts = ("1 file", "2 files", "3 files", "4 files", "5 files", "6 files")
        history = [_check_entry(self.CMD, 0, text) for text in texts]
        self.assertIsNone(coder.find_dithered_command(history))
        alternating = [
            _check_entry(self.CMD, 0, "1 file" if i % 2 else "2 files")
            for i in range(8)
        ]
        self.assertIsNone(coder.find_dithered_command(alternating))

    def test_failing_command_still_detected(self):
        history = [_check_entry(self.CMD, 1, "boom")] * 5
        self.assertEqual(coder.find_dithered_command(history), self.CMD)

    def test_trace_detects_unchanged_loop(self):
        with tempfile.TemporaryDirectory() as d:
            looping = os.path.join(d, "loop.jsonl")
            with open(looping, "w", encoding="utf-8") as f:
                for _ in range(6):
                    f.write(json.dumps(_check_call_event(self.CMD, 0, "1 file")) + "\n")
            self.assertTrue(coder.trace_has_dither(looping))
            changing = os.path.join(d, "changing.jsonl")
            with open(changing, "w", encoding="utf-8") as f:
                for i in range(6):
                    f.write(
                        json.dumps(_check_call_event(self.CMD, 0, "%d files" % i))
                        + "\n"
                    )
            self.assertFalse(coder.trace_has_dither(changing))

    def test_edit_between_checks_resets_trace_guard(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trace.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for _ in range(3):
                    f.write(json.dumps(_check_call_event(self.CMD, 0, "1 file")) + "\n")
                f.write(
                    json.dumps(
                        {
                            "type": "tool_call",
                            "name": "edit_file",
                            "args_preview": "a.py",
                            "ok": True,
                        }
                    )
                    + "\n"
                )
                for _ in range(4):
                    f.write(json.dumps(_check_call_event(self.CMD, 0, "1 file")) + "\n")
            self.assertFalse(coder.trace_has_dither(path))


class TestUnchangedInspectionNotice(unittest.TestCase):
    """#1374: the live run_check streak flags identical repeated output."""

    def test_identical_output_triggers_unchanged_notice(self):
        _install_fake_langchain(self)
        with tempfile.TemporaryDirectory() as worktree:
            subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
            probe = os.path.join(worktree, "probe.txt")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("one\n")
            subprocess.run(["git", "add", "probe.txt"], cwd=worktree, check=True)
            with open(probe, "a", encoding="utf-8") as f:
                f.write("two\n")
            run_check = coder.build_check_tool(worktree)
            command = "git diff --stat"
            first = run_check(command)
            self.assertTrue(first.startswith("exit=0\n"), first)
            self.assertNotIn("DITHERING", first)
            # Changed output is legitimate and resets the streak.
            with open(probe, "a", encoding="utf-8") as f:
                f.write("three\n")
            second = run_check(command)
            self.assertNotIn("DITHERING", second)
            last = second
            for _ in range(5):
                last = run_check(command)
            self.assertIn("DITHERING", last)
            self.assertIn("identical output", last)
            # A different command resets the streak.
            other = run_check("git status")
            self.assertNotIn("DITHERING", other)

    def test_edit_resets_live_guard(self):
        _install_fake_langchain(self)
        with tempfile.TemporaryDirectory() as worktree:
            subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
            probe = os.path.join(worktree, "probe.txt")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("one\n")
            subprocess.run(["git", "add", "probe.txt"], cwd=worktree, check=True)
            with open(probe, "a", encoding="utf-8") as f:
                f.write("two\n")
            written = coder.WriteTracker()
            run_check = coder.build_check_tool(worktree, written)
            command = "git diff --stat"
            # Four unchanged repeats: streak builds but stays under the
            # notice threshold.
            results = [run_check(command) for _ in range(4)]
            self.assertNotIn("DITHERING", results[-1])
            # An actual edit resets the guard even when it does not
            # change this check's output (here: an unrelated new file).
            written.add("unrelated.py")
            written.mutations += 1
            after_edit = run_check(command)
            self.assertNotIn("DITHERING", after_edit)
            # The streak then rebuilds from fresh observations.
            last = after_edit
            for _ in range(5):
                last = run_check(command)
            self.assertIn("DITHERING", last)
            self.assertIn("identical output", last)


if __name__ == "__main__":
    unittest.main()
