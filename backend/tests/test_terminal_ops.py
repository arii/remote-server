"""Terminal takeover + status endpoints (#1387): marker-wrapped pkill
sequencing, hup_exit truth in responses, zellij session parsing."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import ops


def _client():
    app = FastAPI()
    app.include_router(ops.router)
    return TestClient(app)


class RunTakeoverTest(unittest.TestCase):
    def _run(self, stdout="hup_exit=0\n"):
        captured = {}

        def fake_exec_argv(shell):
            captured["shell"] = shell
            return ["docker", "exec", "terminal", "bash", "-c", shell]

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with (
            patch("backend.exec.engine.exec_argv", side_effect=fake_exec_argv),
            patch.object(ops.subprocess, "run", side_effect=fake_run),
        ):
            out = ops.run_takeover()
        return captured, out

    def test_marker_dropped_before_signal_removed_on_no_match(self):
        captured, out = self._run()
        shell = captured["shell"]
        self.assertLess(
            shell.index("touch /tmp/ttyd-takeover"),
            shell.index("pkill -HUP -f '^zellij attach'"),
        )
        self.assertIn("rm -f /tmp/ttyd-takeover", shell)
        self.assertIn("hup_exit=", shell)
        self.assertEqual(out, "hup_exit=0\n")

    def test_wrapper_also_signaled_without_self_match(self):
        import re

        captured, _ = self._run()
        shell = captured["shell"]
        # Bracket form: the pattern must match the wrapper process
        # but NOT this exec shell's own command line, which contains
        # the pattern as literal bracketed text.
        self.assertIn("pkill -HUP -f 'zellij-attach-lo[o]p'", shell)
        self.assertIsNotNone(
            re.search(
                r"zellij-attach-lo[o]p", "bash /usr/local/bin/zellij-attach-loop main"
            )
        )
        self.assertIsNone(re.search(r"zellij-attach-lo[o]p", shell))


class ParseZellijSessionsTest(unittest.TestCase):
    def test_alive_exited_and_blank_lines(self):
        text = (
            "\n"
            "main (Created 11/19/26, 10:00:00 AM)\n"
            "old (Created 11/01/26, 9:00:00 AM) (EXITED)\n"
        )
        got = ops.parse_zellij_sessions(text)
        self.assertEqual([s["name"] for s in got], ["main", "old"])
        self.assertFalse(got[0]["exited"])
        self.assertTrue(got[1]["exited"])

    def test_empty_output(self):
        self.assertEqual(ops.parse_zellij_sessions(""), [])
        self.assertEqual(ops.parse_zellij_sessions(None), [])


class TerminalEndpointsTest(unittest.TestCase):
    def test_takeover_ok_mirrors_marker(self):
        with patch.object(ops, "run_takeover", return_value="hup_exit=0\n"):
            resp = _client().post(
                "/api/terminal/takeover",
                json={"intent": "terminal-takeover", "confirmed": True},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["matched"])

    def test_takeover_no_match_reports_not_ok(self):
        with patch.object(ops, "run_takeover", return_value="hup_exit=1\n"):
            resp = _client().post(
                "/api/terminal/takeover",
                json={"intent": "terminal-takeover", "confirmed": True},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["ok"])
        self.assertFalse(resp.json()["matched"])

    def test_takeover_requires_confirmation(self):
        resp = _client().post(
            "/api/terminal/takeover",
            json={"intent": "terminal-takeover", "confirmed": False},
        )
        self.assertEqual(resp.status_code, 400)

    def test_status_parses_sessions(self):
        sample = (
            "main (Created 11/19/26, 10:00:00 AM)\n"
            "old (Created 11/01/26, 9:00:00 AM) (EXITED)\n"
        )
        with patch.object(ops, "fetch_terminal_sessions", return_value=sample):
            resp = _client().get("/api/terminal/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        names = [s["name"] for s in body["sessions"]]
        self.assertEqual(names, ["main", "old"])
        self.assertFalse(body["sessions"][0]["exited"])
        self.assertTrue(body["sessions"][1]["exited"])

    def test_status_empty_when_no_sessions(self):
        with patch.object(ops, "fetch_terminal_sessions", return_value=""):
            resp = _client().get("/api/terminal/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["sessions"], [])


if __name__ == "__main__":
    unittest.main()
