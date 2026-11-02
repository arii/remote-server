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

    def test_jules_sessions_command_canonical(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('"td --json agent list-sessions; fi"', content)
        self.assertNotIn("jules list-sessions", content)

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

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_jules_sessions_filters_by_selected_repo(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {"gh": "arii/remote-server", "td_cwd": "/srv/remote-server", "host_path": "/home/ari/remote-server"},
            "boomtick": {"gh": "arii/boomtick", "td_cwd": "/srv/repos/boomtick", "host_path": "/home/ari/repos/boomtick"}
        }
        sessions_payload = {
            "sessions": [
                {
                    "id": "sess-rs-1",
                    "title": "Remote Server Fix",
                    "state": "IN_PROGRESS",
                    "sourceContext": {"source": "projects/1/sources/github/repositories/arii/remote-server"}
                },
                {
                    "id": "sess-bt-1",
                    "title": "Boomtick Fix",
                    "state": "IN_PROGRESS",
                    "sourceContext": {"githubRepoContext": {"owner": "arii", "repo": "boomtick"}}
                },
                {
                    "id": "sess-rs-2",
                    "title": "Another Remote Server Fix",
                    "state": "IN_PROGRESS",
                    "sourceContext": {"source": "arii/remote-server"}
                }
            ]
        }
        mock_td_run.return_value = json.dumps(sessions_payload)

        # 1. Query for boomtick
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/buttons/api/jules-sessions?repo=boomtick"
        handler._json = MagicMock()
        handler.do_GET()

        handler._json.assert_called_once()
        res = handler._json.call_args[0][0]
        self.assertEqual(res["repo"], "boomtick")
        self.assertEqual(len(res["sessions"]), 1)
        self.assertEqual(res["sessions"][0]["id"], "sess-bt-1")
        self.assertEqual(res["other"], 2)

        # 2. Query for remote-server
        handler_rs = server.Handler.__new__(server.Handler)
        handler_rs.path = "/buttons/api/jules-sessions?repo=remote-server"
        handler_rs._json = MagicMock()
        handler_rs.do_GET()

        handler_rs._json.assert_called_once()
        res_rs = handler_rs._json.call_args[0][0]
        self.assertEqual(res_rs["repo"], "remote-server")
        self.assertEqual(len(res_rs["sessions"]), 2)
        self.assertEqual([s["id"] for s in res_rs["sessions"]], ["sess-rs-1", "sess-rs-2"])
        self.assertEqual(res_rs["other"], 1)

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_jules_sessions_unknown_provenance_bucketed_not_claimed(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {"gh": "arii/remote-server", "td_cwd": "/srv/remote-server", "host_path": "/home/ari/remote-server"},
            "boomtick": {"gh": "arii/boomtick", "td_cwd": "/srv/repos/boomtick", "host_path": "/home/ari/repos/boomtick"}
        }
        sessions_payload = {
            "sessions": [
                {
                    "id": "sess-no-repo",
                    "title": "boomtick coincidental title but no repo info",
                    "state": "IN_PROGRESS",
                },
                {
                    "id": "sess-rs",
                    "title": "Legit remote-server fix",
                    "state": "IN_PROGRESS",
                    "sourceContext": {"githubRepoContext": {"owner": "arii", "repo": "remote-server"}}
                }
            ]
        }
        mock_td_run.return_value = json.dumps(sessions_payload)

        # Query for boomtick: sess-no-repo should be in unknown, not sessions
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/buttons/api/jules-sessions?repo=boomtick"
        handler._json = MagicMock()
        handler.do_GET()

        handler._json.assert_called_once()
        res = handler._json.call_args[0][0]
        self.assertEqual(res["repo"], "boomtick")
        self.assertEqual(len(res["sessions"]), 0)
        self.assertEqual(len(res["unknown"]), 1)
        self.assertEqual(res["unknown"][0]["id"], "sess-no-repo")
        self.assertEqual(res["other"], 1)

    def test_section_header_labels_include_repo(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('Agent Status: Jules <span class="forrepo" data-forrepo>', content)
        self.assertIn('Jules sessions & Agent Handoff <span class="forrepo" data-forrepo>', content)
        self.assertIn('"Agent sessions (Jules)" + (b.repo ? " — " + b.repo : "")', content)


if __name__ == "__main__":
    unittest.main()
