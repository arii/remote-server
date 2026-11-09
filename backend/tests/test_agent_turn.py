"""Tests for headless provider-CLI turns (stdlib unittest only).

No real agent binary runs here: run_turn tests use /bin/echo and
/bin/sleep via the argv seam, and router tests patch turn_argv to
return /bin/echo.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import agent

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False

from backend.exec import agent_turn

SID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}$"
)
TURN_BODY = {
    "provider": "muse",
    "repo": "testrepo",
    "prompt": "hello",
    "intent": "agent-spawn",
    "confirmed": True,
}


def setUpModule():
    # Hermetic pause state for every test in this module: agent
    # turns park while PAUSED, and the ambient file is the live
    # shared one (often paused by a real viewer). Classes with
    # their own control-file redirect override per-test.
    global _MODULE_CONTROL_TMP, _MODULE_CONTROL_ORIG
    _MODULE_CONTROL_TMP = tempfile.TemporaryDirectory()
    _MODULE_CONTROL_ORIG = os.environ.get("AGENT_CONTROL_STATE_FILE")
    os.environ["AGENT_CONTROL_STATE_FILE"] = os.path.join(
        _MODULE_CONTROL_TMP.name, "control.json"
    )


def tearDownModule():
    if _MODULE_CONTROL_ORIG is None:
        os.environ.pop("AGENT_CONTROL_STATE_FILE", None)
    else:
        os.environ["AGENT_CONTROL_STATE_FILE"] = _MODULE_CONTROL_ORIG
    _MODULE_CONTROL_TMP.cleanup()


def _patch_repo(test):
    import backend.core.git_helpers as git_helpers
    import backend.core.repos as repos
    import backend.core.validation as validation

    test._orig = (
        validation.validate_repo_name,
        git_helpers.get_repo_status,
        repos.resolve_repo_path,
    )
    validation.validate_repo_name = lambda name: name
    git_helpers.get_repo_status = lambda name: {"repo": name}
    repos.resolve_repo_path = lambda name: __import__("pathlib").Path("/tmp") / name
    test.addCleanup(setattr, validation, "validate_repo_name", test._orig[0])
    test.addCleanup(setattr, git_helpers, "get_repo_status", test._orig[1])
    test.addCleanup(setattr, repos, "resolve_repo_path", test._orig[2])


class TurnArgvTest(unittest.TestCase):
    def test_allowlist_is_headless_capable_only(self):
        self.assertEqual(set(agent_turn.PROVIDERS), {"muse", "agy"})

    def test_default_order_is_muse_first(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_DEFAULT_PROVIDER", None)
            self.assertEqual(agent_turn.ordered_providers(), ("muse", "agy"))

    def test_env_flips_default_to_agy(self):
        with mock.patch.dict(os.environ, {"AGENT_DEFAULT_PROVIDER": "agy"}):
            self.assertEqual(agent_turn.ordered_providers(), ("agy", "muse"))

    def test_unknown_or_empty_env_keeps_default_order(self):
        for value in ("pi", "", "  ", "AGY "):
            with mock.patch.dict(os.environ, {"AGENT_DEFAULT_PROVIDER": value}):
                order = agent_turn.ordered_providers()
            if value.strip().lower() in ("muse", "agy"):
                self.assertEqual(order[0], value.strip().lower())
            else:
                self.assertEqual(order, ("muse", "agy"))

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            agent_turn.turn_argv("pi", "/srv/x", "s", "hi", False)

    def test_muse_exec_session_argv(self):
        argv = agent_turn.turn_argv("muse", "/srv/x", "abc123", "hi", False)
        self.assertEqual(argv[-5:], ["muse", "exec", "--session-id", "abc123", "hi"])
        self.assertIn("terminal", argv)
        self.assertIn("-T", argv)

    def test_muse_followup_keeps_session(self):
        first = agent_turn.turn_argv("muse", "/srv/x", "abc123", "hi", False)
        second = agent_turn.turn_argv("muse", "/srv/x", "abc123", "again", True)
        self.assertEqual(first[-4:-1], second[-4:-1])

    def test_agy_first_turn_print(self):
        argv = agent_turn.turn_argv("agy", "/srv/x", "s", "hi", False)
        self.assertEqual(argv[-4:], ["--output-format", "stream-json", "-p", "hi"])
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--conversation", argv)

    def test_colon_prompt_stays_one_argv_element(self):
        # Issue #1036: task text is opaque — colons must ride through
        # as a single argv element (no shell, no YAML shaping) on
        # both providers, so any plain-English prompt launches.
        for provider in ("agy", "muse"):
            argv = agent_turn.turn_argv(
                provider,
                "/srv/x",
                "s",
                "Validate X: fetch the log, then write a spec: done",
                False,
            )
            self.assertIn("Validate X: fetch the log, then write a spec: done", argv)
            self.assertEqual(sum(1 for a in argv if "Validate X:" in a), 1)

    def test_agy_followup_pins_conversation(self):
        argv = agent_turn.turn_argv(
            "agy", "/srv/x", "s", "again", True, conversation_id="cid-1"
        )
        self.assertEqual(
            argv[-6:],
            [
                "--conversation",
                "cid-1",
                "--output-format",
                "stream-json",
                "-p",
                "again",
            ],
        )
        self.assertNotIn("--continue", argv)

    def test_agy_followup_without_conversation_runs_fresh(self):
        # A continued turn with no recorded id starts a new
        # conversation instead of resuming the global most-recent.
        argvs = (
            agent_turn.turn_argv("agy", "/srv/x", "s", "again", True),
            agent_turn.turn_argv(
                "agy", "/srv/x", "s", "again", True, conversation_id=""
            ),
            agent_turn.turn_argv(
                "agy", "/srv/x", "s", "again", True, conversation_id=None
            ),
        )
        for argv in argvs:
            self.assertNotIn("--continue", argv)
            self.assertNotIn("--conversation", argv)
            self.assertIn("stream-json", argv)

    def test_agy_argv_never_uses_global_continue(self):
        for continued, cid in (
            (False, None),
            (True, None),
            (False, "cid-1"),
            (True, "cid-1"),
        ):
            argv = agent_turn.turn_argv(
                "agy", "/srv/x", "s", "hi", continued, conversation_id=cid
            )
            self.assertNotIn("--continue", argv)

    def test_prompt_is_single_argv_element(self):
        argv = agent_turn.turn_argv("muse", "/srv/x", "s", "a; rm -rf /", False)
        self.assertIn("a; rm -rf /", argv)

    def test_terminal_cwd_uses_container_mounts(self):
        # Exec runs in the terminal container: repos live at /srv,
        # never the host-style /home/ari paths (see chat.py td_cwd).
        self.assertEqual(agent_turn.terminal_cwd("boomtick"), "/srv/repos/boomtick")
        self.assertEqual(agent_turn.terminal_cwd("remote-server"), "/srv/remote-server")

    def test_new_session_id_is_full_uuid(self):
        import uuid

        for _ in range(3):
            sid = agent_turn.new_session_id()
            self.assertEqual(sid, str(uuid.UUID(sid)))
            self.assertEqual(len(sid), 36)

    def test_muse_argv_carries_valid_uuid(self):
        import uuid

        sid = agent_turn.new_session_id()
        argv = agent_turn.turn_argv("muse", "/srv/x", sid, "hi", False)
        flag_at = argv.index("--session-id")
        carried = argv[flag_at + 1]
        self.assertEqual(carried, sid)
        uuid.UUID(carried)


class FailureGuidanceTest(unittest.TestCase):
    def test_yaml_line_number_maps_to_prompt_parse(self):
        # Issue #1036: a YAML line number for a natural-language
        # task must name the cause and the retry, never surface raw.
        code, message = agent.failure_guidance(
            "agy", "Agent failed: yaml: line 318: could not find expected ':'"
        )
        self.assertEqual(code, "prompt-parse")
        self.assertIn("agy", message)
        self.assertIn("simpler wording", message)
        self.assertNotIn("line 318", message)

    def test_known_signatures_still_map(self):
        self.assertEqual(
            agent.failure_guidance("muse", "not logged in")[0], "auth-expired"
        )
        self.assertIsNone(agent.failure_guidance("agy", "some new failure"))


class ProbeTest(unittest.TestCase):
    def setUp(self):
        agent_turn.clear_probe_cache()

    def _completed(self, code=0, stdout="ok", stderr=""):
        m = mock.Mock()
        m.returncode = code
        m.stdout = stdout
        m.stderr = stderr
        return m

    def _check_probe_argv(self, run_mock, provider):
        # exec_argv pins the middle (-f path, env file) per
        # environment; head, exec tail, shell, and timeout are exact.
        argv = run_mock.call_args[0][0]
        self.assertEqual(argv[:2], ["docker", "compose"])
        self.assertEqual(
            argv[-6:],
            ["exec", "-T", "terminal", "bash", "-c", "command -v %s" % provider],
        )
        self.assertEqual(run_mock.call_args[1]["timeout"], agent_turn.PROBE_TIMEOUT)

    @mock.patch("backend.exec.agent_turn.subprocess.run")
    def test_probe_available_on_binary_present(self, run):
        run.return_value = self._completed()
        status = asyncio.run(agent_turn.probe_provider("muse", "/srv/remote-server"))
        self.assertTrue(status["available"])
        self.assertEqual(status["name"], "muse")
        self.assertGreater(status["checked_at"], 0)
        self.assertEqual(run.call_count, 1)
        self._check_probe_argv(run, "muse")

    @mock.patch("backend.exec.agent_turn.subprocess.run")
    def test_probe_dead_when_binary_missing(self, run):
        run.return_value = self._completed(code=1, stdout="", stderr="not found")
        status = asyncio.run(agent_turn.probe_provider("muse", "/srv/remote-server"))
        self.assertFalse(status["available"])
        self.assertEqual(run.call_count, 1)
        self._check_probe_argv(run, "muse")

    @mock.patch("backend.exec.agent_turn.subprocess.run")
    def test_probe_fires_no_live_turn(self, run):
        run.return_value = self._completed()
        with mock.patch(
            "backend.exec.agent_turn.turn_argv",
            side_effect=AssertionError("must not build a turn"),
        ):
            status = asyncio.run(agent_turn.probe_provider("agy", "/srv/remote-server"))
        self.assertTrue(status["available"])
        self.assertEqual(run.call_count, 1)
        self._check_probe_argv(run, "agy")

    @mock.patch("backend.exec.agent_turn.subprocess.run")
    def test_probe_dead_on_binary_timeout(self, run):
        import subprocess as sp

        run.side_effect = sp.TimeoutExpired("muse", 15)
        status = asyncio.run(agent_turn.probe_provider("muse", "/srv/remote-server"))
        self.assertFalse(status["available"])
        self.assertIn("binary check failed", status["detail"])
        self._check_probe_argv(run, "muse")

    @mock.patch("backend.exec.agent_turn.subprocess.run")
    def test_status_caches_within_ttl(self, run):
        run.return_value = self._completed()
        asyncio.run(agent_turn.provider_status("/srv/remote-server"))
        asyncio.run(agent_turn.provider_status("/srv/remote-server"))
        self.assertEqual(run.call_count, 2)
        # One binary check per provider; thread order is unsorted.
        self.assertEqual(
            sorted(c[0][0][-1] for c in run.call_args_list),
            ["command -v agy", "command -v muse"],
        )

    def test_probe_rejects_unknown(self):
        with self.assertRaises(ValueError):
            asyncio.run(agent_turn.probe_provider("nope", "/srv/x"))


class AgyStreamParserTest(unittest.TestCase):
    def _feed_all(self, parser, chunks):
        pieces = []
        for chunk in chunks:
            pieces.extend(parser.feed(chunk))
        pieces.extend(parser.flush())
        return pieces

    def test_init_reports_conversation_id(self):
        parser = agent_turn.AgyStreamParser()
        pieces = parser.feed('{"event":"init","conversation_id":"cid-1","init":{}}\n')
        self.assertEqual(pieces, [])
        self.assertEqual(parser.conversation_id, "cid-1")

    def test_agent_deltas_stream_as_answer(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"step_update","step_update":'
                '{"conversation_id":"cid-2","step_type":'
                '"agent_response","text_delta":"PO"}}\n',
                '{"event":"step_update","step_update":'
                '{"conversation_id":"cid-2","step_type":'
                '"agent_response","text_delta":"NG\\n"}}\n',
            ],
        )
        self.assertEqual("".join(out), "PONG\n")
        self.assertEqual(parser.conversation_id, "cid-2")

    def test_result_envelope_does_not_double_yield(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"step_update","step_update":'
                '{"step_type":"agent_response",'
                '"text_delta":"hi"}}\n',
                '{"event":"result","result":'
                '{"conversation_id":"cid-3","status":"SUCCESS",'
                '"response":"hi"}}\n',
            ],
        )
        self.assertEqual(out, ["hi"])
        self.assertEqual(parser.conversation_id, "cid-3")

    def test_result_response_backfills_silent_turn(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"result","result":'
                '{"conversation_id":"cid-4","status":"SUCCESS",'
                '"response":"late answer"}}\n',
            ],
        )
        self.assertEqual(out, ["late answer"])
        self.assertEqual(parser.conversation_id, "cid-4")

    def test_tool_step_lines(self):
        self.assertEqual(
            agent_turn.tool_step_line(
                {
                    "tool_name": "run_command",
                    "tool_info": {
                        "name": "run_command",
                        "parameters": {"CommandLine": "ls /tmp"},
                    },
                }
            ),
            "tool run_command ls /tmp",
        )
        self.assertEqual(agent_turn.tool_step_line({"tool_name": "x"}), "tool x")
        self.assertEqual(agent_turn.tool_step_line({}), "tool")

    def test_tool_active_emits_progress_marker(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"step_update","step_update":'
                '{"conversation_id":"cid-5","step_index":2,'
                '"state":"ACTIVE","step_type":"tool",'
                '"tool_name":"run_command","tool_info":{"name":'
                '"run_command","parameters":{"CommandLine":'
                '"ls /tmp"}}}}\n',
            ],
        )
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], agent_turn.TurnProgress)
        self.assertEqual(out[0].text, "tool run_command ls /tmp")
        # Zero-text: joining chunks never leaks it into the answer.
        self.assertEqual("".join(out), "")

    def test_tool_done_stays_silent(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"step_update","step_update":'
                '{"step_type":"tool","state":"DONE",'
                '"tool_name":"run_command"}}\n',
            ],
        )
        self.assertEqual(out, [])

    def test_non_agent_steps_are_swallowed(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(
            parser,
            [
                '{"event":"step_update","step_update":{"step_type":"user_input"}}\n',
                '{"event":"step_update","step_update":'
                '{"step_type":"tool_call",'
                '"text_delta":"secret"}}\n',
            ],
        )
        self.assertEqual(out, [])

    def test_non_json_passes_through_verbatim(self):
        parser = agent_turn.AgyStreamParser()
        out = self._feed_all(parser, ["hello\n"])
        self.assertEqual(out, ["hello\n"])

    def test_split_line_across_chunks(self):
        parser = agent_turn.AgyStreamParser()
        first = '{"event":"step_update","step_update":{"step_type"'
        second = ':"agent_response","text_delta":"split"}}\n'
        self.assertEqual(parser.feed(first), [])
        self.assertEqual(parser.feed(second), ["split"])

    def test_flush_emits_unterminated_line(self):
        parser = agent_turn.AgyStreamParser()
        self.assertEqual(parser.feed("tail-no-newline"), [])
        self.assertEqual(parser.flush(), ["tail-no-newline"])
        self.assertEqual(parser.flush(), [])


class RunTurnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        # The turn loop parks while PAUSED; the ambient file is
        # the live shared one — point tests at a scratch file.
        self._orig_control = os.environ.get("AGENT_CONTROL_STATE_FILE")
        os.environ["AGENT_CONTROL_STATE_FILE"] = os.path.join(
            self.tmp.name, "control.json"
        )
        self.addCleanup(self._restore_control)
        self.mgr = agent_turn.AgentTurnManager()

    def _restore_control(self):
        if self._orig_control is None:
            os.environ.pop("AGENT_CONTROL_STATE_FILE", None)
        else:
            os.environ["AGENT_CONTROL_STATE_FILE"] = self._orig_control

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def _collect(self, **kw):
        async def go():
            return [c async for c in self.mgr.run_turn(**kw)]

        return asyncio.run(go())

    def test_streams_stdout_chunks(self):
        chunks = self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s1",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        self.assertEqual("".join(chunks), "hello\n")

    def test_result_records_exit_zero(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s2",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        result = self.mgr.pop_result("s2")
        self.assertEqual(result["exit"], 0)
        self.assertIsNone(self.mgr.pop_result("s2"))

    def test_result_records_failure_and_stderr(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s3",
            prompt="hi",
            continued=False,
            argv=["/bin/sh", "-c", "echo oops >&2; exit 3"],
        )
        result = self.mgr.pop_result("s3")
        self.assertEqual(result["exit"], 3)
        self.assertIn("oops", result["stderr"])

    def test_verbose_stderr_does_not_stall_turn(self):
        # Issue #439: stderr past the ~64KB pipe buffer used to
        # deadlock the turn until TURN_TIMEOUT. Stderr now lands in
        # a temp file, so the turn completes fast with stdout
        # intact. TURN_TIMEOUT is patched down so a regression
        # fails in seconds, not after the 600s production budget.
        with (
            mock.patch.object(agent_turn, "TURN_TIMEOUT", 10),
            mock.patch.object(agent_turn, "KILL_GRACE", 1),
        ):
            started = time.time()
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-stderr-flood",
                prompt="hi",
                continued=False,
                argv=[
                    "/bin/sh",
                    "-c",
                    "head -c 204800 /dev/zero | tr '\\0' 'e' >&2; echo hi",
                ],
            )
            elapsed = time.time() - started
        self.assertEqual("".join(chunks), "hi\n")
        self.assertLess(elapsed, 5)
        result = self.mgr.pop_result("s-stderr-flood")
        self.assertEqual(result["exit"], 0)
        self.assertFalse(result["timed_out"])

    def test_failed_verbose_stderr_keeps_final_detail(self):
        # A verbose failing CLI still shows its final error detail:
        # the tail is read from the end of the capture.
        chunks = self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-stderr-fail",
            prompt="hi",
            continued=False,
            argv=[
                "/bin/sh",
                "-c",
                "head -c 204800 /dev/zero | tr '\\0' 'e' >&2; echo boom >&2; exit 3",
            ],
        )
        self.assertEqual(chunks, [])
        result = self.mgr.pop_result("s-stderr-fail")
        self.assertEqual(result["exit"], 3)
        self.assertIn("boom", result["stderr"])
        self.assertLessEqual(len(result["stderr"]), agent_turn.STDERR_TAIL)

    def test_pause_parks_mid_turn(self):
        # Human takeover mid-turn parks the CLI instead of racing
        # it: the transcript names the wheel-handoff, the proc is
        # reaped, and no timeout is recorded.
        with mock.patch.object(agent_turn, "_human_paused", side_effect=[False, True]):
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-paused",
                prompt="hi",
                continued=False,
                argv=["/bin/echo", "hello"],
            )
        self.assertIn("took the wheel", "".join(chunks))
        result = self.mgr.pop_result("s-paused")
        self.assertFalse(result["timed_out"])

    def test_repo_free_turn_gets_browse_guidance(self):
        # The cobrowse brief rides repo-free prompts so a terminal
        # CLI knows the shared-browser contract; repo turns are
        # untouched.
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "ok"]
        ) as argv_mock:
            self._collect(
                provider="muse",
                repo="",
                cwd="/srv",
                session_id="s-browse",
                prompt="take me to x",
                continued=False,
            )
        sent = argv_mock.call_args[0][3]
        self.assertIn("http://buttons:8000/api/browser", sent)
        self.assertIn("take me to x", sent)

    def test_repo_turn_gets_no_browse_guidance(self):
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "ok"]
        ) as argv_mock:
            self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-repo",
                prompt="take me to x",
                continued=False,
            )
        self.assertNotIn("api/browser", argv_mock.call_args[0][3])

    def test_run_turn_marks_session_known(self):
        # Issue #442: only sessions the server ran a turn for may
        # skip the spawn gate. Popping the result must not un-know
        # them — follow-ups arrive after the pop.
        self.assertFalse(self.mgr.is_known("s-known"))
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-known",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        self.assertTrue(self.mgr.is_known("s-known"))
        self.mgr.pop_result("s-known")
        self.assertTrue(self.mgr.is_known("s-known"))
        self.assertFalse(self.mgr.is_known("s-never-ran"))

    def _agy_fixture_argv(self, cid, text):
        lines = [
            '{"event":"init","conversation_id":"%s","init":{}}' % cid,
            '{"event":"step_update","step_update":'
            '{"conversation_id":"%s","step_type":"agent_response",'
            '"text_delta":"%s"}}' % (cid, text),
            '{"event":"result","result":{"conversation_id":"%s",'
            '"status":"SUCCESS","response":"%s"}}' % (cid, text),
        ]
        quoted = " ".join("'%s'" % line for line in lines)
        return ["/bin/sh", "-c", "printf '%%s\\n' %s" % quoted]

    def test_agy_turn_streams_answer_and_pins_conversation(self):
        chunks = self._collect(
            provider="agy",
            repo="r",
            cwd="/tmp",
            session_id="s-agy-1",
            prompt="hi",
            continued=False,
            argv=self._agy_fixture_argv("cid-aaa", "answer one"),
        )
        self.assertEqual("".join(chunks), "answer one")
        self.assertEqual(self.mgr.agy_conversation("s-agy-1"), "cid-aaa")

    def test_agy_sessions_pin_distinct_conversations(self):
        # Issue #440: two sessions' agy turns never share context —
        # each follow-up resumes its own recorded conversation.
        self._collect(
            provider="agy",
            repo="r",
            cwd="/tmp",
            session_id="s-agy-A",
            prompt="hi",
            continued=False,
            argv=self._agy_fixture_argv("cid-A", "A"),
        )
        self._collect(
            provider="agy",
            repo="r",
            cwd="/tmp",
            session_id="s-agy-B",
            prompt="hi",
            continued=False,
            argv=self._agy_fixture_argv("cid-B", "B"),
        )
        seen = {}

        def spy(provider, cwd, session_id, prompt, continued, conversation_id=None):
            seen[session_id] = (continued, conversation_id)
            return ["/bin/echo", "ok"]

        with mock.patch.object(agent_turn, "turn_argv", side_effect=spy):
            self._collect(
                provider="agy",
                repo="r",
                cwd="/tmp",
                session_id="s-agy-A",
                prompt="again",
                continued=True,
            )
            self._collect(
                provider="agy",
                repo="r",
                cwd="/tmp",
                session_id="s-agy-B",
                prompt="again",
                continued=True,
            )
        self.assertEqual(seen["s-agy-A"], (True, "cid-A"))
        self.assertEqual(seen["s-agy-B"], (True, "cid-B"))

    def test_agy_followup_without_mapping_runs_fresh(self):
        seen = {}

        def spy(provider, cwd, session_id, prompt, continued, conversation_id=None):
            seen["conversation_id"] = conversation_id
            return ["/bin/echo", "ok"]

        with mock.patch.object(agent_turn, "turn_argv", side_effect=spy):
            chunks = self._collect(
                provider="agy",
                repo="r",
                cwd="/tmp",
                session_id="s-agy-nomap",
                prompt="again",
                continued=True,
            )
        self.assertIsNone(seen["conversation_id"])
        self.assertEqual("".join(chunks), "ok\n")

    def test_agy_registry_evicts_oldest_first(self):
        with mock.patch.object(agent_turn, "KNOWN_SESSIONS_MAX", 2):
            self.mgr._remember_agy("a", "cid-a")
            self.mgr._remember_agy("b", "cid-b")
            self.mgr._remember_agy("c", "cid-c")
        self.assertIsNone(self.mgr.agy_conversation("a"))
        self.assertEqual(self.mgr.agy_conversation("c"), "cid-c")

    def test_ledgers_completion(self):
        self._collect(
            provider="agy",
            repo="r",
            cwd="/tmp",
            session_id="s4",
            prompt="hi",
            continued=True,
            argv=["/bin/echo", "yo"],
        )
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        kinds = [e.get("kind") for e in entries]
        self.assertIn("agent-turn", kinds)

    def _turn_entry(self, session_id):
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            for line in fh:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if (
                    entry.get("kind") == "agent-turn"
                    and entry.get("session_id") == session_id
                ):
                    return entry
        self.fail(f"no agent-turn entry for {session_id}")

    def test_ledgers_decision_detail(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-dec",
            prompt="list files",
            continued=False,
            argv=["/bin/echo", "yo"],
        )
        entry = self._turn_entry("s-dec")
        self.assertEqual(entry.get("prompt"), "list files")
        detail = entry.get("detail", "")
        for needle in ("asked: list files", "ran: muse turn", "verdict: exit 0"):
            self.assertIn(needle, detail)
        self.assertNotIn("error", entry)

    def test_ledger_failure_carries_reason(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-fail",
            prompt="hi",
            continued=False,
            argv=["/bin/sh", "-c", "echo oops >&2; exit 3"],
        )
        entry = self._turn_entry("s-fail")
        self.assertIn("exit 3", entry.get("error", ""))
        self.assertIn("oops", entry.get("error", ""))
        self.assertIn("verdict", entry.get("detail", ""))

    def test_abort_kills_inflight(self):
        async def go():
            task = asyncio.create_task(
                self._agen(session_id="s5", argv=["/bin/sleep", "30"])
            )
            await asyncio.sleep(0.3)
            killed = await self.mgr.abort("s5")
            await task
            return killed

        self.assertTrue(asyncio.run(go()))

    async def _agen(self, **kw):
        async for _ in self.mgr.run_turn(
            provider="muse", repo="r", cwd="/tmp", prompt="hi", continued=False, **kw
        ):
            pass

    def test_abort_unknown_is_false(self):
        self.assertFalse(asyncio.run(self.mgr.abort("nope")))

    def test_second_turn_aborts_first_and_owns_result(self):
        # Single-flight (issue #441): a second turn on one session
        # kills the in-flight CLI; the retired turn raises
        # TurnSuperseded instead of storing, so pop_result returns
        # the survivor's record.
        async def go():
            first = asyncio.create_task(
                self._agen(session_id="s-race", argv=["/bin/sleep", "30"])
            )
            await asyncio.sleep(0.3)
            prior = self.mgr._running.get("s-race")
            self.assertIsNotNone(prior)
            chunks = [
                c
                async for c in self.mgr.run_turn(
                    provider="muse",
                    repo="r",
                    cwd="/tmp",
                    session_id="s-race",
                    prompt="again",
                    continued=True,
                    argv=["/bin/echo", "second"],
                )
            ]
            with self.assertRaises(agent_turn.TurnSuperseded):
                await first
            return chunks, prior

        chunks, prior = asyncio.run(go())
        self.assertEqual("".join(chunks), "second\n")
        self.assertIsNotNone(prior.poll())
        result = self.mgr.pop_result("s-race")
        self.assertEqual(result["exit"], 0)
        self.assertIsNone(self.mgr.pop_result("s-race"))
        self.assertNotIn("s-race", self.mgr._running)

    def test_concurrent_starts_leave_one_owner(self):
        # Two turns started together: exactly one streams to a
        # stored result; the loser raises TurnSuperseded and the
        # slot ends free.
        async def go():
            async def collect(tag):
                return "".join(
                    [
                        c
                        async for c in self.mgr.run_turn(
                            provider="muse",
                            repo="r",
                            cwd="/tmp",
                            session_id="s-duo",
                            prompt=tag,
                            continued=False,
                            argv=["/bin/sh", "-c", "sleep 0.2; echo %s" % tag],
                        )
                    ]
                )

            return await asyncio.gather(
                collect("aaa"), collect("bbb"), return_exceptions=True
            )

        results = asyncio.run(go())
        texts = sorted(r for r in results if not isinstance(r, Exception))
        losers = [r for r in results if isinstance(r, agent_turn.TurnSuperseded)]
        self.assertEqual(len(texts), 1)
        self.assertEqual(len(losers), 1)
        stored = self.mgr.pop_result("s-duo")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["exit"], 0)
        self.assertNotIn("s-duo", self.mgr._running)

    def test_abandoned_superseded_turn_keeps_newer_result(self):
        # Generation-guarded drop-on-abandon (issue #441): closing
        # the retired turn's stream after its successor stored must
        # not drop the newer turn's result.
        async def go():
            gen = self.mgr.run_turn(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-old-new",
                prompt="hi",
                continued=False,
                argv=[
                    "/bin/sh",
                    "-c",
                    "head -c 70000 /dev/zero | tr '\\0' 'y'; sleep 30",
                ],
            )
            chunk = await gen.__anext__()
            self.assertEqual(len(chunk), agent_turn.READ_CHUNK)
            second = "".join(
                [
                    c
                    async for c in self.mgr.run_turn(
                        provider="muse",
                        repo="r",
                        cwd="/tmp",
                        session_id="s-old-new",
                        prompt="again",
                        continued=True,
                        argv=["/bin/echo", "new"],
                    )
                ]
            )
            self.assertEqual(second, "new\n")
            await gen.aclose()

        asyncio.run(go())
        result = self.mgr.pop_result("s-old-new")
        self.assertIsNotNone(result)
        self.assertEqual(result["exit"], 0)

    def test_sequential_same_session_turns_each_store(self):
        # Single-flight must not break back-to-back turns on one
        # session: each completes, stores, and pops its own record.
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-seq",
            prompt="one",
            continued=False,
            argv=["/bin/echo", "one"],
        )
        first = self.mgr.pop_result("s-seq")
        self.assertEqual(first["exit"], 0)
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-seq",
            prompt="two",
            continued=True,
            argv=["/bin/echo", "two"],
        )
        second = self.mgr.pop_result("s-seq")
        self.assertEqual(second["exit"], 0)

    def test_abandoned_generator_reaps_proc_and_frees_session(self):
        # Client disconnect (New session, navigate away, tab close)
        # closes the SSE generator mid-stream: the CLI must die
        # promptly and the session slot must free, mirroring chat's
        # GeneratorExit kill (chat.py "SSE zombie, #294").
        async def go():
            gen = self.mgr.run_turn(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-abandon",
                prompt="hi",
                continued=False,
                argv=[
                    "/bin/sh",
                    "-c",
                    "head -c 70000 /dev/zero | tr '\\0' 'y'; sleep 30",
                ],
            )
            # Text-mode read(READ_CHUNK) yields only full 64KB
            # buffers, so the fake emits 70KB then hangs: the first
            # chunk arrives while the CLI is still running.
            chunk = await gen.__anext__()
            self.assertEqual(len(chunk), agent_turn.READ_CHUNK)
            proc = self.mgr._running.get("s-abandon")
            self.assertIsNotNone(proc)
            await gen.aclose()
            alive_after_close = proc.poll() is None
            if alive_after_close:
                proc.kill()  # pre-fix hermeticity: never leak sleep
                proc.wait()
            return alive_after_close

        self.assertFalse(asyncio.run(go()))
        self.assertNotIn("s-abandon", self.mgr._running)
        self.assertFalse(asyncio.run(self.mgr.abort("s-abandon")))

    def test_abandoned_turn_leaves_no_result(self):
        # Drop-on-abandon (issue #535): a stream closed
        # mid-turn must not leave a _results entry behind.
        async def go():
            gen = self.mgr.run_turn(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-abandon-nores",
                prompt="hi",
                continued=False,
                argv=[
                    "/bin/sh",
                    "-c",
                    "head -c 70000 /dev/zero | tr '\\0' 'y'; sleep 30",
                ],
            )
            await gen.__anext__()
            await gen.aclose()

        asyncio.run(go())
        self.assertNotIn("s-abandon-nores", self.mgr._results)
        self.assertIsNone(self.mgr.pop_result("s-abandon-nores"))

    def test_abandoned_turn_drops_unpopped_result(self):
        # A completed turn's unpopped entry for a session is
        # dropped when a later turn on the same session is
        # abandoned (issue #535).
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-reuse",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        self.assertIn("s-reuse", self.mgr._results)

        async def go():
            gen = self.mgr.run_turn(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-reuse",
                prompt="again",
                continued=True,
                argv=[
                    "/bin/sh",
                    "-c",
                    "head -c 70000 /dev/zero | tr '\\0' 'y'; sleep 30",
                ],
            )
            await gen.__anext__()
            await gen.aclose()

        asyncio.run(go())
        self.assertNotIn("s-reuse", self.mgr._results)
        self.assertNotIn("s-reuse", self.mgr._result_at)
        self.assertIsNone(self.mgr.pop_result("s-reuse"))

    def test_result_sweep_evicts_only_stale_entries(self):
        # TTL backstop (issue #535): entries older than
        # RESULT_TTL are evicted; fresh ones survive, and pop
        # clears the timestamp so the side table cannot leak.
        self.mgr._results["s-old"] = {"exit": 0}
        self.mgr._result_at["s-old"] = time.time() - agent_turn.RESULT_TTL - 1
        self.mgr._results["s-fresh"] = {"exit": 0}
        self.mgr._result_at["s-fresh"] = time.time()
        self.mgr._sweep_results()
        self.assertNotIn("s-old", self.mgr._results)
        self.assertNotIn("s-old", self.mgr._result_at)
        self.assertIn("s-fresh", self.mgr._results)
        self.assertEqual(self.mgr.pop_result("s-fresh"), {"exit": 0})
        self.assertNotIn("s-fresh", self.mgr._result_at)

    def test_completed_turn_sweeps_stale_entries(self):
        # The store sweeps opportunistically (chat.py pi_runs
        # precedent): completing a turn evicts entries that
        # aged past RESULT_TTL (issue #535).
        self.mgr._results["s-stale"] = {"exit": 0}
        self.mgr._result_at["s-stale"] = time.time() - agent_turn.RESULT_TTL - 1
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-new",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        self.assertNotIn("s-stale", self.mgr._results)
        self.assertNotIn("s-stale", self.mgr._result_at)
        self.assertIn("s-new", self.mgr._results)
        self.assertIn("s-new", self.mgr._result_at)

    def test_hung_turn_times_out_and_reaps(self):
        with (
            mock.patch.object(agent_turn, "TURN_TIMEOUT", 1),
            mock.patch.object(agent_turn, "KILL_GRACE", 1),
        ):
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s7",
                prompt="hi",
                continued=False,
                argv=["/bin/sleep", "30"],
            )
        self.assertEqual(chunks, [])
        result = self.mgr.pop_result("s7")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["truncated"])

    def test_runaway_output_is_capped_and_killed(self):
        with mock.patch.object(agent_turn, "TURN_OUTPUT_MAX", 16):
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s6",
                prompt="hi",
                continued=False,
                argv=["/bin/sh", "-c", "yes ABCDEFGH | head -c 100000"],
            )
        self.assertEqual(len("".join(chunks)), 16)
        result = self.mgr.pop_result("s6")
        self.assertTrue(result["truncated"])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class AgentRouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        # Hermetic control state: the route refuses while PAUSED,
        # and the ambient file is the live shared one (often
        # paused by a real viewer) — never read it in tests.
        self._orig_control = os.environ.get("AGENT_CONTROL_STATE_FILE")
        os.environ["AGENT_CONTROL_STATE_FILE"] = os.path.join(
            self.tmp.name, "control.json"
        )
        self.addCleanup(self._restore_control)
        _patch_repo(self)
        app = FastAPI()
        app.include_router(agent.router)
        self.client = TestClient(app)

    def _restore_control(self):
        if self._orig_control is None:
            os.environ.pop("AGENT_CONTROL_STATE_FILE", None)
        else:
            os.environ["AGENT_CONTROL_STATE_FILE"] = self._orig_control

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def test_providers_lists_allowlist(self):
        async def fake_status(cwd):
            return []

        with mock.patch.object(
            agent.agent_turn, "provider_status", side_effect=fake_status
        ):
            resp = self.client.get("/api/agent/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()["providers"]), {"muse", "agy"})

    def test_providers_reports_status(self):
        async def fake_status(cwd):
            return [
                {
                    "name": "muse",
                    "available": False,
                    "checked_at": 1700000000,
                    "detail": "auth revoked",
                }
            ]

        with mock.patch.object(
            agent.agent_turn, "provider_status", side_effect=fake_status
        ):
            resp = self.client.get("/api/agent/providers")
        body = resp.json()
        self.assertEqual(body["providers"], ["muse", "agy"])
        self.assertEqual(len(body["status"]), 1)
        self.assertFalse(body["status"][0]["available"])

    def test_providers_endpoint_respects_default_env(self):
        async def fake_status(cwd):
            return []

        with (
            mock.patch.dict(os.environ, {"AGENT_DEFAULT_PROVIDER": "agy"}),
            mock.patch.object(
                agent.agent_turn, "provider_status", side_effect=fake_status
            ),
        ):
            resp = self.client.get("/api/agent/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["providers"], ["agy", "muse"])

    def test_turn_rejects_unknown_provider(self):
        body = dict(TURN_BODY, provider="pi")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_turn_rejects_empty_prompt(self):
        body = dict(TURN_BODY, prompt="  ")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_new_conversation_requires_confirm(self):
        body = dict(TURN_BODY)
        body.pop("confirmed")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_turn_streams_token_and_done(self):
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "AGY-OK"]
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        body = resp.text
        self.assertIn("event: token", body)
        self.assertIn("AGY-OK", body)
        done = [
            json.loads(line[6:])
            for line in body.splitlines()
            if line.startswith("data: ")
        ][-1]
        self.assertIn("AGY-OK", done["text"])
        self.assertTrue(SID_RE.match(done["session_id"]))

    def test_minted_session_id_requires_confirm(self):
        # Issue #442: a client-minted id the server never issued
        # must not skip the spawn gate.
        body = {
            "provider": "muse",
            "repo": "testrepo",
            "session_id": agent_turn.new_session_id(),
            "prompt": "again",
        }
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_unknown_session_id_with_confirm_spawns_fresh(self):
        # Unknown id + confirm is allowed, but runs as a fresh
        # (non-continued) spawn — and registers the id.
        sid = agent_turn.new_session_id()
        body = dict(TURN_BODY, session_id=sid)
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "ok1"]
        ) as argv_mock:
            resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok1", resp.text)
        self.assertFalse(argv_mock.call_args[0][4])
        self.assertTrue(agent_turn.manager.is_known(sid))

    def test_followup_turn_skips_confirm(self):
        # Genuine follow-up on a server-known session stays free.
        sid = agent_turn.new_session_id()
        first = dict(TURN_BODY, session_id=sid)
        second = {
            "provider": "muse",
            "repo": "testrepo",
            "session_id": sid,
            "prompt": "again",
        }
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "ok2"]
        ) as argv_mock:
            resp1 = self.client.post("/api/agent/turn", json=first)
            resp = self.client.post("/api/agent/turn", json=second)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok2", resp.text)
        continued_flags = [call[0][4] for call in argv_mock.call_args_list]
        self.assertEqual(continued_flags, [False, True])

    def test_agy_followup_pins_recorded_conversation(self):
        # Issue #440 end to end: the router's continued agy turn
        # resumes the session's recorded conversation id.
        sid = agent_turn.new_session_id()
        first = dict(TURN_BODY, provider="agy", session_id=sid)
        second = {
            "provider": "agy",
            "repo": "testrepo",
            "session_id": sid,
            "prompt": "again",
        }
        self.addCleanup(agent_turn.manager._agy.pop, sid, None)
        seen = {}

        def spy(provider, cwd, session_id, prompt, continued, conversation_id=None):
            seen[session_id] = (continued, conversation_id)
            return ["/bin/echo", "ok-agy"]

        with mock.patch.object(agent_turn, "turn_argv", side_effect=spy):
            resp1 = self.client.post("/api/agent/turn", json=first)
            agent_turn.manager._remember_agy(sid, "cid-router")
            resp = self.client.post("/api/agent/turn", json=second)
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok-agy", resp.text)
        self.assertEqual(seen[sid], (True, "cid-router"))

    def test_turn_failure_sends_error_event(self):
        with mock.patch.object(
            agent_turn,
            "turn_argv",
            return_value=["/bin/sh", "-c", "echo bad >&2; exit 2"],
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("bad", resp.text)

    def test_unrelated_error_has_no_guidance(self):
        with mock.patch.object(
            agent_turn,
            "turn_argv",
            return_value=["/bin/sh", "-c", "echo some weird boom >&2; exit 2"],
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("some weird boom", resp.text)
        self.assertNotIn('"code"', resp.text)
        self.assertNotIn('"detail"', resp.text)

    def _failed_turn(self, stderr_line):
        with mock.patch.object(
            agent_turn,
            "turn_argv",
            return_value=["/bin/sh", "-c", f"echo {stderr_line} >&2; exit 1"],
        ):
            return self.client.post("/api/agent/turn", json=TURN_BODY)

    def test_expired_creds_send_guidance(self):
        resp = self._failed_turn("Error: access token expired, please login again")
        self.assertIn("event: error", resp.text)
        self.assertIn('"code": "auth-expired"', resp.text)
        self.assertIn("switch provider", resp.text)
        self.assertIn('"detail"', resp.text)
        self.assertIn("token expired", resp.text)

    def test_access_denied_sends_guidance(self):
        resp = self._failed_turn("Error: access denied for key")
        self.assertIn('"code": "access-denied"', resp.text)
        self.assertIn("switch provider", resp.text)

    def test_binary_missing_sends_guidance(self):
        resp = self._failed_turn("executable file not found in PATH")
        self.assertIn('"code": "binary-missing"', resp.text)
        self.assertIn("switch provider", resp.text)

    def test_truncated_turn_sends_done_with_flag(self):
        async def _fake(*args, **kwargs):
            yield "partial-"

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={"exit": -15, "stderr": "", "truncated": True},
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: done", resp.text)
        self.assertIn('"truncated": true', resp.text)
        self.assertNotIn("event: error", resp.text)

    def test_timed_out_turn_sends_error_event(self):
        async def _fake(*args, **kwargs):
            yield "partial-"
            return
            yield  # pragma: no cover - make this an async generator

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={
                    "exit": -15,
                    "stderr": "",
                    "truncated": False,
                    "timed_out": True,
                },
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("timed out after", resp.text)
        self.assertNotIn("event: done", resp.text)

    def test_superseded_turn_sends_error_event(self):
        # Single-flight (issue #441): the retired turn's stream
        # reports superseded without popping the owner's result.
        async def _victim(*args, **kwargs):
            yield "partial-"
            raise agent_turn.TurnSuperseded("s-victim")
            yield  # pragma: no cover - make this an async generator

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _victim),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                side_effect=AssertionError("must not pop another turn's record"),
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("turn-superseded", resp.text)
        self.assertIn("partial-", resp.text)
        self.assertNotIn("event: done", resp.text)

    def test_abort_requires_session(self):
        resp = self.client.post("/api/agent/abort", json={})
        self.assertEqual(resp.status_code, 400)

    def test_abort_idle_is_not_killed(self):
        resp = self.client.post("/api/agent/abort", json={"session_id": "idle1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True, "killed": False})

    def test_turn_repo_free_uses_neutral_cwd(self):
        # Browsing turns are not tied to a repo: empty repo skips
        # validation and runs in the neutral stack-root workdir (a
        # trusted checkout — bare /srv is untrusted and the CLIs
        # refuse to start there).
        body = dict(TURN_BODY, repo="")
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "ok-free"]
        ) as argv_mock:
            resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok-free", resp.text)
        self.assertEqual(argv_mock.call_args[0][1], agent_turn.TERMINAL_STACK)

    def test_turn_refuses_while_human_driving(self):
        # A turn fired while PAUSED would drive against the
        # takeover: refuse fast with the wheel message, no spawn.
        import backend.api.routers.agent_control as control

        paused = control.AgentControlState(state="PAUSED_BY_USER")
        with (
            mock.patch.object(control, "read_control_state", return_value=paused),
            mock.patch.object(
                agent_turn, "turn_argv", side_effect=AssertionError("must not spawn")
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertEqual(resp.status_code, 409)
        self.assertIn("wheel", resp.json()["detail"])


class TurnTimeoutEnvTest(unittest.TestCase):
    def test_default_budget_is_thirty_minutes(self):
        self.assertEqual(agent_turn.TURN_TIMEOUT, 1800)

    def test_unset_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_TURN_TIMEOUT", None)
            self.assertEqual(agent_turn.turn_timeout(), 1800)

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"AGENT_TURN_TIMEOUT": "600"}):
            self.assertEqual(agent_turn.turn_timeout(), 600)

    def test_garbage_and_non_positive_fall_back(self):
        for raw in ("", "  ", "ten", "1.5", "0", "-30"):
            with mock.patch.dict(os.environ, {"AGENT_TURN_TIMEOUT": raw}):
                self.assertEqual(agent_turn.turn_timeout(), 1800, raw)

    def test_timeout_message_uses_effective_budget(self):
        detail, error = agent_turn.turn_decisions(
            "muse", "hi", False, None, time.time(), "", "", False, True, timeout=600
        )
        self.assertIn("600s", detail)
        self.assertIn("600s", error)


class TurnWarningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        self.mgr = agent_turn.AgentTurnManager()

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def _collect(self, **kw):
        async def go():
            return [c async for c in self.mgr.run_turn(**kw)]

        return asyncio.run(go())

    def test_silent_turn_warns_once_then_times_out(self):
        with (
            mock.patch.object(agent_turn, "TURN_TIMEOUT", 6),
            mock.patch.object(agent_turn, "TURN_WARNING_LEAD", 3),
            mock.patch.object(agent_turn, "KILL_GRACE", 1),
            mock.patch.object(
                agent_turn,
                "rescue_partial_work",
                return_value={
                    "branch": None,
                    "commit": None,
                    "committed": False,
                    "pushed": False,
                },
            ),
        ):
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-warn",
                prompt="hi",
                continued=False,
                argv=["/bin/sleep", "30"],
            )
        warnings = [c for c in chunks if isinstance(c, agent_turn.TurnWarning)]
        self.assertEqual(len(warnings), 1)
        self.assertLessEqual(warnings[0].seconds_left, 3)
        self.assertGreaterEqual(warnings[0].seconds_left, 0)
        # Zero-text: joining the stream is unaffected.
        self.assertEqual("".join(chunks), "")
        result = self.mgr.pop_result("s-warn")
        self.assertTrue(result["timed_out"])

    def test_no_warning_when_budget_shorter_than_lead(self):
        with (
            mock.patch.object(agent_turn, "TURN_TIMEOUT", 1),
            mock.patch.object(agent_turn, "KILL_GRACE", 1),
            mock.patch.object(
                agent_turn,
                "rescue_partial_work",
                return_value={
                    "branch": None,
                    "commit": None,
                    "committed": False,
                    "pushed": False,
                },
            ),
        ):
            chunks = self._collect(
                provider="muse",
                repo="r",
                cwd="/tmp",
                session_id="s-no-warn",
                prompt="hi",
                continued=False,
                argv=["/bin/sleep", "30"],
            )
        self.assertEqual(chunks, [])
        self.assertTrue(self.mgr.pop_result("s-no-warn")["timed_out"])

    def test_fast_turn_never_warns(self):
        chunks = self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-fast",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        self.assertEqual("".join(chunks), "hello\n")
        self.assertFalse(self.mgr.pop_result("s-fast")["timed_out"])


class RescueTest(unittest.TestCase):
    def _completed(self, code=0, out=""):
        return subprocess.CompletedProcess(
            args=["git"], returncode=code, stdout=out, stderr=""
        )

    def test_dirty_tree_commits_and_pushes(self):
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return self._completed(out="feat-x\n")
            if args[:1] == ["status"]:
                return self._completed(out=" M a.txt\n")
            if args[:1] == ["rev-parse"]:
                return self._completed(out="abc123\n")
            return self._completed()

        record = agent_turn.rescue_partial_work("/srv/repos/x", _exec=fake)
        self.assertEqual(
            record,
            {"branch": "feat-x", "commit": "abc123", "committed": True, "pushed": True},
        )
        self.assertIn(["add", "-A"], calls)
        self.assertIn(["commit", "-m", agent_turn.RESCUE_COMMIT_MESSAGE], calls)
        self.assertIn(["push", "origin", "feat-x"], calls)

    def test_clean_tree_reports_head_without_push(self):
        def fake(args, timeout):
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return self._completed(out="main\n")
            if args[:1] == ["status"]:
                return self._completed(out="")
            if args[:1] == ["rev-parse"]:
                return self._completed(out="head1\n")
            raise AssertionError("must not commit: %r" % (args,))

        record = agent_turn.rescue_partial_work("/srv/x", _exec=fake)
        self.assertEqual(
            record,
            {"branch": "main", "commit": "head1", "committed": False, "pushed": False},
        )

    def test_detached_head_commits_without_push(self):
        def fake(args, timeout):
            if args[:2] == ["rev-parse", "--abbrev-ref"]:
                return self._completed(out="HEAD\n")
            if args[:1] == ["status"]:
                return self._completed(out=" M a.txt\n")
            if args[:1] == ["rev-parse"]:
                return self._completed(out="sha9\n")
            if args[:1] == ["push"]:
                raise AssertionError("must not push detached")
            return self._completed()

        record = agent_turn.rescue_partial_work("/srv/x", _exec=fake)
        self.assertTrue(record["committed"])
        self.assertFalse(record["pushed"])

    def test_runner_exception_is_best_effort(self):
        def boom(args, timeout):
            raise OSError("docker down")

        record = agent_turn.rescue_partial_work("/srv/x", _exec=boom)
        self.assertEqual(
            record,
            {"branch": None, "commit": None, "committed": False, "pushed": False},
        )

    def test_zero_timeout_returns_empty_record(self):
        def boom(args, timeout):
            raise AssertionError("must not run past the cap")

        record = agent_turn.rescue_partial_work("/srv/x", timeout=0, _exec=boom)
        self.assertFalse(record["committed"])
        self.assertFalse(record["pushed"])

    def test_real_git_repo_gets_wip_commit(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = tmp.name

        def git(*args):
            return subprocess.run(
                ["git", "-C", repo, *args], capture_output=True, text=True, timeout=30
            )

        self.assertEqual(git("init", "-b", "main").returncode, 0)
        git("config", "user.email", "rescue@test")
        git("config", "user.name", "rescue")
        with open(os.path.join(repo, "note.txt"), "w") as fh:
            fh.write("v1\n")
        self.assertEqual(git("add", "-A").returncode, 0)
        self.assertEqual(git("commit", "-m", "base").returncode, 0)
        with open(os.path.join(repo, "note.txt"), "a") as fh:
            fh.write("partial work\n")
        record = agent_turn.rescue_partial_work(
            repo,
            _exec=lambda args, timeout: subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            ),
        )
        self.assertEqual(record["branch"], "main")
        self.assertTrue(record["committed"])
        # No origin remote: the push fails, best-effort.
        self.assertFalse(record["pushed"])
        log = git("log", "--format=%s", "-1")
        self.assertEqual(log.stdout.strip(), agent_turn.RESCUE_COMMIT_MESSAGE)
        self.assertEqual(git("status", "--porcelain").stdout, "")
        self.assertEqual(git("rev-parse", "HEAD").stdout.strip(), record["commit"])


class TimeoutRescueWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        self.mgr = agent_turn.AgentTurnManager()

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def test_timeout_stores_rescue_and_budget(self):
        salvage = {
            "branch": "feat-y",
            "commit": "c1" * 20,
            "committed": True,
            "pushed": True,
        }
        with (
            mock.patch.object(agent_turn, "TURN_TIMEOUT", 1),
            mock.patch.object(agent_turn, "KILL_GRACE", 1),
            mock.patch.object(
                agent_turn, "rescue_partial_work", return_value=salvage
            ) as run,
        ):

            async def go():
                return [
                    c
                    async for c in self.mgr.run_turn(
                        provider="muse",
                        repo="r",
                        cwd="/tmp",
                        session_id="s-salv",
                        prompt="hi",
                        continued=False,
                        argv=["/bin/sleep", "30"],
                    )
                ]

            asyncio.run(go())
        run.assert_called_once_with("/tmp")
        result = self.mgr.pop_result("s-salv")
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["timeout"], 1)
        self.assertEqual(result["rescue"], salvage)
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        entry = [e for e in entries if e.get("session_id") == "s-salv"][0]
        self.assertEqual(entry["rescue"], salvage)
        self.assertIn("salvaged:", entry["detail"])
        self.assertIn("feat-y", entry["detail"])

    def test_success_never_rescues(self):
        with mock.patch.object(
            agent_turn,
            "rescue_partial_work",
            side_effect=AssertionError("no rescue on success"),
        ):

            async def go():
                return [
                    c
                    async for c in self.mgr.run_turn(
                        provider="muse",
                        repo="r",
                        cwd="/tmp",
                        session_id="s-ok",
                        prompt="hi",
                        continued=False,
                        argv=["/bin/echo", "done"],
                    )
                ]

            chunks = asyncio.run(go())
        self.assertEqual("".join(chunks), "done\n")
        result = self.mgr.pop_result("s-ok")
        self.assertFalse(result["timed_out"])
        self.assertIsNone(result["rescue"])


class SpecPromptTest(unittest.TestCase):
    def test_spec_task_appends_template(self):
        self.assertTrue(agent_turn.is_spec_task("write a design spec for hrm#9825"))
        self.assertTrue(agent_turn.is_spec_task("WRITE A SPEC for the queue"))
        sent = agent_turn.spec_prompt("write a design spec for x")
        self.assertTrue(sent.startswith("write a design spec for x"))
        self.assertIn("Follow this spec template exactly", sent)
        self.assertIn("## ", sent)

    def test_non_spec_prompt_passes_through(self):
        self.assertFalse(agent_turn.is_spec_task("implement x"))
        self.assertFalse(agent_turn.is_spec_task(""))
        self.assertEqual(agent_turn.spec_prompt("implement x"), "implement x")
        self.assertEqual(agent_turn.spec_prompt(""), "")

    def test_missing_template_degrades_to_noop(self):
        with mock.patch.object(agent_turn, "spec_template", return_value=""):
            self.assertEqual(
                agent_turn.spec_prompt("write a design spec"), "write a design spec"
            )

    def test_run_turn_sends_spec_template_to_cli(self):
        sent = agent_turn.spec_prompt("write a design spec for x")
        composed = agent_turn.guard_prompt(sent)
        self.assertIn("Follow this spec template exactly", composed)
        self.assertIn(agent_turn.FILE_HYGIENE_GUARDRAIL, composed)


class GuardPromptTest(unittest.TestCase):
    def test_guardrail_appends_to_prompt(self):
        guarded = agent_turn.guard_prompt("implement x")
        self.assertTrue(guarded.startswith("implement x"))
        self.assertIn(agent_turn.FILE_HYGIENE_GUARDRAIL, guarded)
        self.assertIn("revert", guarded)

    def test_run_turn_sends_guarded_prompt_to_cli(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = os.path.join(tmp.name, "ledger.jsonl")
        with mock.patch.dict(os.environ, {"BUTTONS_LEDGER_PATH": ledger}):
            mgr = agent_turn.AgentTurnManager()
            with mock.patch.object(
                agent_turn, "turn_argv", return_value=["/bin/echo", "ok"]
            ) as argv_mock:

                async def go():
                    return [
                        c
                        async for c in mgr.run_turn(
                            provider="muse",
                            repo="r",
                            cwd="/tmp",
                            session_id="s-guard",
                            prompt="do it",
                            continued=False,
                        )
                    ]

                asyncio.run(go())
        sent = argv_mock.call_args[0][3]
        self.assertIn("do it", sent)
        self.assertIn(agent_turn.FILE_HYGIENE_GUARDRAIL, sent)
        # The ledger keeps the raw user prompt, not the boilerplate.
        with open(ledger) as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
        entry = [e for e in entries if e.get("session_id") == "s-guard"][0]
        self.assertEqual(entry["prompt"], "do it")


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class TurnSurvivalRouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        _patch_repo(self)
        app = FastAPI()
        app.include_router(agent.router)
        self.client = TestClient(app)

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def test_warning_marker_renders_warning_event(self):
        async def _fake(*args, **kwargs):
            yield agent_turn.TurnWarning(120)
            yield "partial-"

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={
                    "exit": 0,
                    "stderr": "",
                    "truncated": False,
                    "timed_out": False,
                },
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: warning", resp.text)
        self.assertIn('"seconds_left": 120', resp.text)
        # The marker never leaks into the transcript.
        done = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ][-1]
        self.assertEqual(done["text"], "partial-")

    def test_progress_marker_renders_progress_event(self):
        async def _fake(*args, **kwargs):
            yield agent_turn.TurnProgress("tool run_command ls /tmp")
            yield "partial-"

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={
                    "exit": 0,
                    "stderr": "",
                    "truncated": False,
                    "timed_out": False,
                },
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: progress", resp.text)
        self.assertIn("tool run_command ls /tmp", resp.text)
        # The marker never leaks into the transcript.
        done = [
            json.loads(line[6:])
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ][-1]
        self.assertEqual(done["text"], "partial-")

    def test_timeout_error_carries_session_and_salvage(self):
        async def _fake(*args, **kwargs):
            return
            yield  # pragma: no cover - make this an async generator

        salvage = {
            "branch": "feat-z",
            "commit": "d" * 40,
            "committed": True,
            "pushed": False,
        }
        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={
                    "exit": -15,
                    "stderr": "",
                    "truncated": False,
                    "timed_out": True,
                    "timeout": 1800,
                    "rescue": salvage,
                },
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("turn timed out after 30m", resp.text)
        self.assertIn('"code": "turn-timeout"', resp.text)
        self.assertIn('"session_id"', resp.text)
        self.assertIn('"salvage"', resp.text)
        self.assertIn("feat-z", resp.text)
        self.assertIn("pushed", resp.text)

    def test_timeout_minutes_follow_effective_budget(self):
        async def _fake(*args, **kwargs):
            return
            yield  # pragma: no cover - make this an async generator

        with (
            mock.patch.object(agent_turn.manager, "run_turn", _fake),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={
                    "exit": -15,
                    "stderr": "",
                    "truncated": False,
                    "timed_out": True,
                    "timeout": 600,
                    "rescue": None,
                },
            ),
        ):
            resp = self.client.post("/api/agent/turn", json=TURN_BODY)
        self.assertIn("turn timed out after 10m", resp.text)
        self.assertNotIn('"salvage"', resp.text)


class SessionSnapshotTest(unittest.TestCase):
    """Live transcript buffer + reattach snapshot (issue #856)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        self.mgr = agent_turn.AgentTurnManager()

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def _collect(self, **kw):
        async def go():
            return [c async for c in self.mgr.run_turn(**kw)]

        return asyncio.run(go())

    def test_unknown_id_reports_not_known(self):
        snap = self.mgr.session_snapshot("never-seen")
        self.assertEqual(snap["session_id"], "never-seen")
        self.assertFalse(snap["known"])
        self.assertFalse(snap["running"])
        self.assertEqual(snap["transcript"], "")
        self.assertFalse(snap["transcript_truncated"])
        self.assertIsNone(snap["finished"])
        self.assertFalse(self.mgr.is_running("never-seen"))

    def test_completed_turn_buffers_transcript_and_verdict(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-snap",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "hello"],
        )
        snap = self.mgr.session_snapshot("s-snap")
        self.assertTrue(snap["known"])
        self.assertFalse(snap["running"])
        self.assertEqual(snap["transcript"], "hello\n")
        self.assertIsNotNone(snap["finished"])
        self.assertEqual(snap["finished"]["exit"], 0)
        self.assertFalse(snap["finished"]["timed_out"])
        # The durable verdict survives the owning stream's pop.
        self.assertIsNotNone(self.mgr.pop_result("s-snap"))
        kept = self.mgr.session_snapshot("s-snap")
        self.assertIsNotNone(kept["finished"])
        self.assertEqual(kept["transcript"], "hello\n")

    def test_failed_turn_records_stderr_in_verdict(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-snap-fail",
            prompt="hi",
            continued=False,
            argv=["/bin/sh", "-c", "echo oops >&2; exit 3"],
        )
        fin = self.mgr.session_snapshot("s-snap-fail")["finished"]
        self.assertEqual(fin["exit"], 3)
        self.assertIn("oops", fin["stderr"])

    def test_new_turn_resets_buffer_and_verdict(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-reset",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "first"],
        )
        self.assertEqual(self.mgr.session_snapshot("s-reset")["transcript"], "first\n")
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-reset",
            prompt="again",
            continued=True,
            argv=["/bin/echo", "second"],
        )
        snap = self.mgr.session_snapshot("s-reset")
        self.assertEqual(snap["transcript"], "second\n")
        self.assertEqual(snap["finished"]["exit"], 0)

    def test_note_guidance_attaches_to_finish_record(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-guide",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "ok"],
        )
        self.mgr.note_guidance(
            "s-guide", "auth-expired", "muse access expired", "token bad"
        )
        fin = self.mgr.session_snapshot("s-guide")["finished"]
        self.assertEqual(fin["guidance_code"], "auth-expired")
        self.assertEqual(fin["guidance_message"], "muse access expired")
        self.assertEqual(fin["guidance_detail"], "token bad")
        # No record, no crash.
        self.mgr.note_guidance("never-seen", "x", "y")

    def test_sweep_drops_stale_live_entries(self):
        self._collect(
            provider="muse",
            repo="r",
            cwd="/tmp",
            session_id="s-stale",
            prompt="hi",
            continued=False,
            argv=["/bin/echo", "old"],
        )
        old = time.time() - agent_turn.RESULT_TTL - 10
        with self.mgr._lock:
            self.mgr._live_at["s-stale"] = old
        self.mgr._sweep_results()
        snap = self.mgr.session_snapshot("s-stale")
        self.assertEqual(snap["transcript"], "")
        self.assertIsNone(snap["finished"])
        # Still server-known: only the live state sweeps.
        self.assertTrue(snap["known"])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class SessionReattachRouterTest(unittest.TestCase):
    """GET /api/agent/session (issue #856)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        _patch_repo(self)
        app = FastAPI()
        app.include_router(agent.router)
        self.client = TestClient(app)

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def test_unknown_id_reports_not_known(self):
        resp = self.client.get(
            "/api/agent/session", params={"session_id": agent_turn.new_session_id()}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["known"])
        self.assertFalse(body["running"])
        self.assertEqual(body["transcript"], "")
        self.assertIsNone(body["finished"])

    def test_empty_session_id_is_rejected(self):
        resp = self.client.get("/api/agent/session", params={"session_id": ""})
        self.assertEqual(resp.status_code, 400)

    def test_finished_turn_is_reattachable(self):
        with mock.patch.object(
            agent_turn, "turn_argv", return_value=["/bin/echo", "REATTACH-OK"]
        ):
            turn = self.client.post("/api/agent/turn", json=TURN_BODY)
        done = [
            json.loads(line[6:])
            for line in turn.text.splitlines()
            if line.startswith("data: ")
        ][-1]
        sid = done["session_id"]
        resp = self.client.get("/api/agent/session", params={"session_id": sid})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["known"])
        self.assertFalse(body["running"])
        self.assertIn("REATTACH-OK", body["transcript"])
        self.assertEqual(body["finished"]["exit"], 0)

    def test_failure_guidance_lands_on_finish_record(self):
        # Unknown id + spawn confirm runs fresh; the failure's
        # guidance must land on the finish record so a reattached
        # console renders the same card the live stream showed.
        sid = agent_turn.new_session_id()
        body = dict(TURN_BODY, session_id=sid)
        argv = ["/bin/sh", "-c", "echo 'error: invalid token' >&2; exit 1"]
        with mock.patch.object(agent_turn, "turn_argv", return_value=argv):
            turn = self.client.post("/api/agent/turn", json=body)
        self.assertIn('"code": "auth-expired"', turn.text)
        resp = self.client.get("/api/agent/session", params={"session_id": sid})
        self.assertEqual(resp.status_code, 200)
        fin = resp.json()["finished"]
        self.assertEqual(fin["exit"], 1)
        self.assertEqual(fin["guidance_code"], "auth-expired")
        self.assertIn("muse", fin["guidance_message"])
