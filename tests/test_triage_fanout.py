"""triage_fanout empty-model guard tests (#620).

Blank/unset NEBIUS_MODEL must fall back to the pinned model, and
provider failures must exit 1 with a one-line stderr message (no
traceback). No network: the provider call is stubbed to raise.

Needs the deep-agent venv (langchain import at module top); skipped
under system python.
Run: deep-agent/.venv/bin/python tests/test_triage_fanout.py
"""
import importlib
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

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
        env = {"NEBIUS_API_KEY": "dummy"}
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

    def test_api_key_missing_unchanged(self):
        argv = ["triage_fanout.py", "arii/remote-server"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(triage_fanout, "load_dotenv"), \
                redirect_stdout(out), redirect_stderr(err):
            code = triage_fanout.main()
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue(), "NEBIUS_API_KEY is not set\n")
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
