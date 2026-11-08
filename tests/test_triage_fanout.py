"""triage_fanout empty-model guard tests (#620) + prefer selection (#622).

Blank/unset NEBIUS_MODEL must fall back to the pinned model, and
provider failures must exit 1 with a one-line stderr message (no
traceback). No network: the provider call is stubbed to raise.
--prefer=ollama|nebius|auto selects the backend; auto (default) uses
Nebius when keyed, else the local model with a stderr note.

Needs the deep-agent venv (langchain import at module top); skipped
under system python.
Run: deep-agent/.venv/bin/python tests/test_triage_fanout.py
"""
import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

try:
    import triage_fanout
    HAS_AGENT_DEPS = True
except ImportError:
    triage_fanout = None
    HAS_AGENT_DEPS = False

PIN = "nvidia/Nemotron-3_5-Lightning"


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestModelResolution(unittest.TestCase):
    def _model_id_with(self, value=None):
        """Re-import MODEL_ID with NEBIUS_MODEL set to value (None=unset)."""
        env = dict(os.environ)
        env.pop("NEBIUS_MODEL", None)
        if value is not None:
            env["NEBIUS_MODEL"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            importlib.reload(triage_fanout)
            return triage_fanout.MODEL_ID

    def test_blank_model_env_falls_back_to_pin(self):
        self.assertEqual(self._model_id_with(""), PIN)

    def test_whitespace_model_env_falls_back_to_pin(self):
        self.assertEqual(self._model_id_with("   "), PIN)

    def test_unset_model_env_uses_pin(self):
        self.assertEqual(self._model_id_with(None), PIN)

    def test_explicit_model_env_honored(self):
        self.assertEqual(
            self._model_id_with("acme/custom"), "acme/custom")


class Fake404(Exception):
    """Mimics openai.APIStatusError: status_code + multi-line message."""
    status_code = 404

    def __str__(self):
        return ("Error code: 404 - {'error': 'The model does not exist'}\n"
                "Request id: abc123")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestMainFailures(unittest.TestCase):
    def _run_main(self, env_extra, invoke_effect):
        argv = ["triage_fanout.py", "arii/remote-server"]
        env = {"NEBIUS_API_KEY": "dummy",
               "DEEP_AGENT_STATE_DIR": tempfile.mkdtemp()}
        env.update(env_extra)
        agent = mock.Mock()
        agent.invoke.side_effect = invoke_effect
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                mock.patch.object(
                    triage_fanout.model_router, "resolve_llm",
                    return_value={"provider": "nebius", "model": "x",
                                  "base_url": "y"}), \
                mock.patch.object(
                    triage_fanout.model_router, "chat_kwargs",
                    side_effect=lambda s: {"model": s["model"],
                                           "base_url": s["base_url"]}), \
                mock.patch.object(triage_fanout, "ChatOpenAI") as chat_cls, \
                mock.patch.object(
                    triage_fanout, "create_deep_agent",
                    return_value=agent), \
                mock.patch.object(triage_fanout.ledger, "record") \
                as record_mock, \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        return code, out.getvalue(), err.getvalue(), chat_cls, record_mock

    def test_provider_404_exits_one_with_single_line(self):
        code, stdout, stderr, chat_cls, record_mock = self._run_main(
            {"NEBIUS_MODEL": ""}, Fake404())
        self.assertEqual(code, 1)
        self.assertEqual(
            stderr,
            "fanout: Nebius error 404: Error code: 404 - "
            "{'error': 'The model does not exist'}\n")
        self.assertEqual(stderr.count("\n"), 1)
        self.assertNotIn("Traceback", stdout)
        self.assertNotIn("Traceback", stderr)
        record_mock.assert_not_called()

    def test_blank_model_never_reaches_provider(self):
        _, _, _, chat_cls, _ = self._run_main(
            {"NEBIUS_MODEL": ""}, Fake404())
        kwargs = chat_cls.call_args
        self.assertIsNotNone(kwargs)
        self.assertEqual(kwargs.kwargs["model"], PIN)

    def test_unset_model_never_reaches_provider(self):
        _, _, _, chat_cls, _ = self._run_main({}, Fake404())
        self.assertEqual(chat_cls.call_args.kwargs["model"], PIN)

    def test_plain_error_without_status_code(self):
        code, stdout, stderr, _, _ = self._run_main(
            {"NEBIUS_MODEL": ""}, RuntimeError("boom\nsecond line"))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "fanout: error: boom\n")
        self.assertNotIn("Traceback", stdout + stderr)

    def test_unresolved_model_guard(self):
        argv = ["triage_fanout.py", "arii/remote-server"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(
                    os.environ, {"NEBIUS_API_KEY": "dummy"},
                    clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                mock.patch.object(
                    triage_fanout.model_router, "resolve_llm",
                    return_value={}), \
                mock.patch.object(
                    triage_fanout.model_router, "_env",
                    return_value=""), \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        self.assertEqual(code, 1)
        self.assertEqual(
            err.getvalue(),
            "fanout: no model resolved "
            "(NEBIUS_MODEL is blank and no default)\n")

    def test_api_key_missing_explicit_nebius_refuses(self):
        # #622 changed the keyless default (auto -> local); only an
        # explicit --prefer=nebius still refuses cleanly.
        argv = ["triage_fanout.py", "--prefer=nebius", "arii/remote-server"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "NEBIUS_API_KEY is not set\n")
        self.assertEqual(out.getvalue(), "")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestPreferSelection(unittest.TestCase):
    LOCAL_SPEC = {"provider": "ollama", "model": "qwen3:8b",
                  "base_url": "http://ollama:11434"}
    CLOUD_SPEC = {"provider": "nebius", "model": "x", "base_url": "y"}
    NOTE = "fanout: no Nebius key — triage on local model (weaker)\n"

    def _run_main(self, argv, env, spec):
        env = {**env, "DEEP_AGENT_STATE_DIR": tempfile.mkdtemp()}
        agent = mock.Mock()
        scout = types.SimpleNamespace(
            tool_calls=[{"name": "task"}], content="scout report")
        final = types.SimpleNamespace(tool_calls=[], content="done")
        agent.invoke.return_value = {"messages": [scout, final]}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                mock.patch.object(
                    triage_fanout.model_router, "resolve_llm",
                    return_value=spec) as resolve_mock, \
                mock.patch.object(triage_fanout, "ChatOpenAI") as chat_cls, \
                mock.patch("langchain_ollama.ChatOllama") as ollama_cls, \
                mock.patch.object(
                    triage_fanout, "create_deep_agent",
                    return_value=agent), \
                mock.patch.object(
                    triage_fanout.ledger, "record") as record_mock, \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        return (code, out.getvalue(), err.getvalue(), resolve_mock,
                chat_cls, ollama_cls, record_mock)

    def test_extract_prefer_defaults_to_auto(self):
        self.assertEqual(
            triage_fanout.extract_prefer(["triage_fanout.py"]), "auto")
        self.assertEqual(
            triage_fanout.extract_prefer(
                ["triage_fanout.py", "arii/remote-server"]),
            "auto")

    def test_extract_prefer_explicit_honored(self):
        for value in ("ollama", "nebius", "auto"):
            self.assertEqual(
                triage_fanout.extract_prefer(
                    ["triage_fanout.py", f"--prefer={value}"]),
                value)

    def test_extract_prefer_normalizes_case_and_space(self):
        self.assertEqual(
            triage_fanout.extract_prefer(
                ["triage_fanout.py", "--prefer= Ollama "]),
            "ollama")

    def test_extract_prefer_invalid_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            triage_fanout.extract_prefer(
                ["triage_fanout.py", "--prefer=bogus"])
        self.assertIn("bad --prefer='bogus'", str(ctx.exception))

    def test_keyed_auto_uses_nebius_pin(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_cls,
         record_mock) = self._run_main(
            ["triage_fanout.py", "arii/remote-server"],
            {"NEBIUS_API_KEY": "dummy"}, self.CLOUD_SPEC)
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer="nebius")
        self.assertEqual(chat_cls.call_args.kwargs["model"], PIN)
        self.assertEqual(
            chat_cls.call_args.kwargs["base_url"],
            "https://api.studio.nebius.com/v1")
        ollama_cls.assert_not_called()
        self.assertNotIn("no Nebius key", stderr)
        self.assertEqual(record_mock.call_args.args[1], f"nebius:{PIN}")

    def test_keyless_auto_uses_local_spec_with_note(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_cls,
         record_mock) = self._run_main(
            ["triage_fanout.py", "arii/remote-server"],
            {}, dict(self.LOCAL_SPEC))
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer=None)
        chat_cls.assert_not_called()
        kwargs = ollama_cls.call_args.kwargs
        self.assertEqual(kwargs["model"], "qwen3:8b")
        self.assertEqual(kwargs["base_url"], "http://ollama:11434")
        self.assertEqual(kwargs["model_kwargs"], {"think": False})
        self.assertIn(self.NOTE, stderr)
        self.assertEqual(
            record_mock.call_args.args[1], "ollama:qwen3:8b")

    def test_explicit_prefer_ollama_honored_without_note(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_cls, _) = \
            self._run_main(
                ["triage_fanout.py", "--prefer=ollama",
                 "arii/remote-server"],
                {"NEBIUS_API_KEY": "dummy"}, dict(self.LOCAL_SPEC))
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer="ollama")
        ollama_cls.assert_called_once()
        chat_cls.assert_not_called()
        self.assertNotIn("no Nebius key", stderr)

    def test_prefer_bogus_is_usage_error(self):
        argv = ["triage_fanout.py", "--prefer=bogus", "arii/remote-server"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(
                    os.environ, {"NEBIUS_API_KEY": "dummy"},
                    clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        self.assertEqual(code, 2)
        self.assertIn("bad --prefer='bogus'", err.getvalue())


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestTopActionParse(unittest.TestCase):
    def test_present_line_parses(self):
        text = "counts: 3 open\nTOP ACTION: Merge #12\nmore"
        self.assertEqual(triage_fanout.parse_top_action(text),
                         "Merge #12")

    def test_case_and_whitespace_tolerant(self):
        self.assertEqual(
            triage_fanout.parse_top_action("  top action:  Rebase main  "),
            "Rebase main")

    def test_missing_is_none(self):
        self.assertIsNone(triage_fanout.parse_top_action("no action here"))

    def test_malformed_is_none(self):
        self.assertIsNone(triage_fanout.parse_top_action("TOP ACTION"))
        self.assertIsNone(triage_fanout.parse_top_action("TOP ACTION:   "))
        self.assertIsNone(triage_fanout.parse_top_action(""))
        self.assertIsNone(triage_fanout.parse_top_action(None))

    def test_first_line_wins(self):
        text = "TOP ACTION: First\nTOP ACTION: Second"
        self.assertEqual(triage_fanout.parse_top_action(text), "First")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestScoutResultsJoin(unittest.TestCase):
    REPOS = ["arii/remote-server", "arii/boomtick"]

    def _messages(self):
        ai = types.SimpleNamespace(
            tool_calls=[
                {"name": "task", "id": "c1",
                 "args": {"description": "Triage arii/remote-server now",
                         "subagent_type": "repo_scout"}},
                {"name": "task", "id": "c2",
                 "args": {"description": "Triage arii/boomtick now",
                         "subagent_type": "repo_scout"}},
            ],
            content="")
        t1 = types.SimpleNamespace(
            tool_call_id="c1", status="success",
            content="counts ok\nTOP ACTION: Merge #1")
        t2 = types.SimpleNamespace(
            tool_call_id="c2", status="success",
            content="counts ok, nothing urgent")
        final = types.SimpleNamespace(tool_calls=[], content="merged")
        return [ai, t1, t2, final]

    def test_per_repo_top_action_and_ok(self):
        out = triage_fanout.scout_results_from_messages(
            self._messages(), self.REPOS)
        self.assertEqual(out, {
            "arii/remote-server": {"top_action": "Merge #1", "ok": True},
            "arii/boomtick": {"top_action": None, "ok": True},
        })

    def test_error_status_marks_not_ok(self):
        ai, t1, t2, final = self._messages()
        t2.status = "error"
        t2.content = "fanout: scout exploded"
        out = triage_fanout.scout_results_from_messages(
            [ai, t1, t2, final], self.REPOS)
        self.assertFalse(out["arii/boomtick"]["ok"])

    def test_missing_result_is_not_ok(self):
        ai, t1, _, final = self._messages()
        out = triage_fanout.scout_results_from_messages(
            [ai, t1, final], self.REPOS)
        # Called but never answered: flag it, don't silently drop it.
        self.assertEqual(out["arii/boomtick"],
                         {"top_action": None, "ok": False})
        self.assertIn("arii/remote-server", out)

    def test_dict_messages_supported(self):
        messages = [
            {"tool_calls": [{"name": "task", "id": "c9",
                             "args": {"description": "go arii/boomtick"}}],
             "content": ""},
            {"tool_call_id": "c9", "content": "TOP ACTION: Ship it"},
        ]
        out = triage_fanout.scout_results_from_messages(
            messages, self.REPOS)
        self.assertEqual(out["arii/boomtick"]["top_action"], "Ship it")
        self.assertTrue(out["arii/boomtick"]["ok"])

    def test_unknown_repo_falls_back(self):
        ai = types.SimpleNamespace(
            tool_calls=[{"name": "task", "id": "c1",
                         "args": {"description": "triage somewhere"}}],
            content="")
        t1 = types.SimpleNamespace(tool_call_id="c1",
                                   content="TOP ACTION: X")
        out = triage_fanout.scout_results_from_messages(
            [ai, t1], self.REPOS)
        self.assertEqual(out["unknown"]["top_action"], "X")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestScoutProgressMiddleware(unittest.TestCase):
    REPOS = ["arii/remote-server", "arii/boomtick"]

    def _wrap(self):
        mw = triage_fanout.scout_progress_middleware(self.REPOS)
        self.assertEqual(len(mw), 1)
        return mw[0].wrap_tool_call

    def _req(self, name="task", description="Triage arii/remote-server"):
        return types.SimpleNamespace(
            tool_call={"name": name,
                       "args": {"description": description,
                               "subagent_type": "repo_scout"}})

    def test_started_done_lines_bracket_handler(self):
        wrap = self._wrap()
        out = io.StringIO()

        def handler(req):
            # Started line precedes completion: live, not post-hoc.
            self.assertIn("fanout: scout arii/remote-server started",
                          out.getvalue())
            return "scout report"

        with redirect_stdout(out):
            result = wrap(self._req(), handler)
        self.assertEqual(result, "scout report")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("started"))
        self.assertIn("done", lines[1])
        self.assertIn("ok", lines[1])
        self.assertRegex(lines[1], r"\d+\.\d+s")

    def test_error_line_and_reraise(self):
        wrap = self._wrap()
        out = io.StringIO()

        def boom(req):
            raise RuntimeError("scout blew up")

        with redirect_stdout(out):
            with self.assertRaises(RuntimeError):
                wrap(self._req(), boom)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("error", lines[1])

    def test_non_task_calls_pass_through_silently(self):
        wrap = self._wrap()
        out = io.StringIO()
        with redirect_stdout(out):
            result = wrap(self._req(name="triage_counts"), lambda r: "3")
        self.assertEqual(result, "3")
        self.assertEqual(out.getvalue(), "")

    def test_unknown_repo_labeled(self):
        wrap = self._wrap()
        out = io.StringIO()
        with redirect_stdout(out):
            wrap(self._req(description="triage somewhere"), lambda r: "x")
        self.assertIn("fanout: scout unknown started", out.getvalue())

    def test_task_event_lands_in_trace(self):
        import json
        import tempfile

        import observe

        with tempfile.TemporaryDirectory() as d:
            rec = observe.TraceRecorder(os.path.join(d, "t.jsonl"))
            rec.run_start("triage_fanout.py", "fanout-x", "m")
            _, _, wrap_mw = observe.trace_middleware(rec)
            wrap_mw.wrap_tool_call(self._req(), lambda r: "report")
            rec.run_end(True)
            rec.close()
            events = [json.loads(line) for line in
                      open(os.path.join(d, "t.jsonl"))]
        kinds = [(e["type"], e.get("name")) for e in events]
        self.assertIn(("tool_call", "task"), kinds)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestFanoutWiring(unittest.TestCase):
    def _run_main(self, messages, repos=("arii/remote-server",
                                        "arii/boomtick")):
        tmp = tempfile.mkdtemp()
        agent = mock.Mock()
        agent.invoke.return_value = {"messages": messages}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                sys, "argv", ["triage_fanout.py", *repos]), \
                mock.patch.dict(os.environ,
                                {"NEBIUS_API_KEY": "dummy",
                                 "DEEP_AGENT_STATE_DIR": tmp},
                                clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                mock.patch.object(
                    triage_fanout.model_router, "resolve_llm",
                    return_value={"provider": "nebius", "model": "x",
                                  "base_url": "y"}), \
                mock.patch.object(
                    triage_fanout.model_router, "chat_kwargs",
                    side_effect=lambda s: {"model": s["model"],
                                           "base_url": s["base_url"]}), \
                mock.patch.object(triage_fanout, "ChatOpenAI"), \
                mock.patch.object(
                    triage_fanout, "create_deep_agent",
                    return_value=agent) as create_mock, \
                mock.patch.object(
                    triage_fanout.ledger, "record") as record_mock, \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        return (code, out.getvalue(), err.getvalue(), create_mock,
                record_mock, tmp)

    def _scout_messages(self):
        ai = types.SimpleNamespace(
            tool_calls=[
                {"name": "task", "id": "c1",
                 "args": {"description": "Triage arii/remote-server"}},
                {"name": "task", "id": "c2",
                 "args": {"description": "Triage arii/boomtick"}},
            ],
            content="")
        t1 = types.SimpleNamespace(
            tool_call_id="c1", content="TOP ACTION: Merge #1")
        t2 = types.SimpleNamespace(
            tool_call_id="c2", content="TOP ACTION: Cut a release")
        final = types.SimpleNamespace(tool_calls=[], content="merged!")
        return [ai, t1, t2, final]

    def test_middleware_and_trace_wired(self):
        (code, stdout, stderr, create_mock, record_mock,
         tmp) = self._run_main(self._scout_messages())
        self.assertEqual(code, 0)
        middleware = create_mock.call_args.kwargs["middleware"]
        # 3 observe hooks + 1 scout-progress hook.
        self.assertEqual(len(middleware), 4)
        traces = os.path.join(tmp, "traces")
        files = os.listdir(traces)
        self.assertEqual(len(files), 1)
        self.assertIn("trace: ", stderr)

    def test_scout_results_in_ledger_extra(self):
        (_, _, _, _, record_mock,
         _) = self._run_main(self._scout_messages())
        extra = record_mock.call_args.kwargs["extra"]
        self.assertEqual(extra["scout_results"], {
            "arii/remote-server": {"top_action": "Merge #1", "ok": True},
            "arii/boomtick": {"top_action": "Cut a release",
                              "ok": True},
        })
        # Aggregate row keys intact.
        self.assertEqual(extra["task_delegations"], 2)
        self.assertEqual(extra["repos"],
                         ["arii/remote-server", "arii/boomtick"])

    def test_existing_output_lines_unchanged(self):
        (_, stdout, _, _, _, _) = self._run_main(self._scout_messages())
        self.assertIn("task_delegations: 2", stdout)
        self.assertIn("FINAL: merged!", stdout)


if __name__ == "__main__":
    unittest.main()
