#!/usr/bin/env python3
import json
import unittest
from unittest.mock import patch, MagicMock

import buttons.briefing_summary as bs
import buttons.server as server


class TestBriefingJulesDedup(unittest.TestCase):
    def test_briefing_deduplicates_sessions_by_id(self):
        # Two sessions with same ID should collapse into one
        data = {
            "pr_list": [],
            "gh": {},
            "conf": None,
            "reviews": {},
            "reviews_err": {},
            "board": None,
            "board_err": "",
            "runs": None,
            "runs_err": "",
            "jobs": {},
            "prs_err": "",
            "conf_err": "",
            "jules": [
                {
                    "id": "session-123",
                    "title": "[cleanup] Correct misclassified production and development dependencies",
                    "state": "IN_PROGRESS",
                    "createTime": "2026-11-01T10:00:00Z",
                    "sourceContext": {
                        "githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}
                    },
                },
                {
                    "id": "session-123",
                    "title": "[cleanup] Correct misclassified production and development dependencies",
                    "state": "IN_PROGRESS",
                    "createTime": "2026-11-01T10:00:00Z",
                    "sourceContext": {
                        "githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}
                    },
                },
            ],
            "jules_err": "",
        }
        res = bs.analyze(data)
        items = res["jules"]["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("[cleanup] Correct misclassified", items[0])

    def test_briefing_preserves_distinct_sessions_with_same_title(self):
        # Two sessions with same title but different IDs should both be preserved
        data = {
            "pr_list": [],
            "gh": {},
            "conf": None,
            "reviews": {},
            "reviews_err": {},
            "board": None,
            "board_err": "",
            "runs": None,
            "runs_err": "",
            "jobs": {},
            "prs_err": "",
            "conf_err": "",
            "jules": [
                {
                    "id": "session-123",
                    "title": "[cleanup] Correct misclassified production and development dependencies",
                    "state": "IN_PROGRESS",
                    "createTime": "2026-11-01T10:00:00Z",
                    "sourceContext": {
                        "githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}
                    },
                },
                {
                    "id": "session-456",
                    "title": "[cleanup] Correct misclassified production and development dependencies",
                    "state": "IN_PROGRESS",
                    "createTime": "2026-11-01T11:00:00Z",
                    "sourceContext": {
                        "githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}
                    },
                },
            ],
            "jules_err": "",
        }
        res = bs.analyze(data)
        items = res["jules"]["items"]
        self.assertEqual(len(items), 2)

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_jules_sessions_api_deduplicates_by_id(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "tech-dancer": {
                "gh": "arii/tech-dancer",
                "td_cwd": "/srv/repos/tech-dancer",
                "host_path": "/home/ari/repos/tech-dancer",
            }
        }
        sessions_payload = {
            "sessions": [
                {
                    "id": "sess-1",
                    "title": "Task 1",
                    "state": "IN_PROGRESS",
                    "updateTime": "2026-11-01T10:00:00Z",
                    "sourceContext": {"githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}},
                },
                {
                    "id": "sess-1",
                    "title": "Task 1",
                    "state": "IN_PROGRESS",
                    "updateTime": "2026-11-01T10:00:00Z",
                    "sourceContext": {"githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}},
                },
                {
                    "id": "sess-2",
                    "title": "Task 1",
                    "state": "IN_PROGRESS",
                    "updateTime": "2026-11-01T11:00:00Z",
                    "sourceContext": {"githubRepoContext": {"owner": "arii", "repo": "tech-dancer"}},
                },
            ]
        }
        mock_td_run.return_value = json.dumps(sessions_payload)

        handler = server.Handler.__new__(server.Handler)
        handler.path = "/buttons/api/jules-sessions?repo=tech-dancer"
        handler._json = MagicMock()

        handler.do_GET()

        handler._json.assert_called_once()
        args, kwargs = handler._json.call_args
        res = args[0]
        self.assertEqual(res["repo"], "tech-dancer")
        self.assertEqual(len(res["sessions"]), 2)
        self.assertEqual(res["sessions"][0]["id"], "sess-1")
        self.assertEqual(res["sessions"][1]["id"], "sess-2")


if __name__ == "__main__":
    unittest.main()
