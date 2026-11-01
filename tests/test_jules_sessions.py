#!/usr/bin/env python3
import json
import unittest
from unittest.mock import patch, MagicMock

import buttons.server as server


class TestJulesSessions(unittest.TestCase):
    def test_friendly_error_jules_no_such_command(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("No such command ['\"]?(?:jules|agent)['\"]?", content)

    def test_jules_sessions_command_has_fallback(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('"td --json agent list-sessions || "', content)
        self.assertIn('"td --json jules list-sessions; fi"', content)

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_jules_sessions_no_such_command_graceful_notice(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {"gh": "arii/remote-server", "td_cwd": "/srv/remote-server", "host_path": "/home/ari/remote-server"}
        }
        mock_td_run.side_effect = RuntimeError("Error: No such command 'jules'.")

        handler = server.Handler.__new__(server.Handler)
        handler.path = "/buttons/api/jules-sessions?repo=remote-server"
        handler._json = MagicMock()

        handler.do_GET()

        handler._json.assert_called_once()
        args, kwargs = handler._json.call_args
        res = args[0]
        self.assertEqual(res["repo"], "remote-server")
        self.assertEqual(res["sessions"], [])
        self.assertIn("this td version can't list Jules sessions", res["notice"])


if __name__ == "__main__":
    unittest.main()
