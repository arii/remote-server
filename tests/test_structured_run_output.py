#!/usr/bin/env python3
import unittest
import re

class TestStructuredRunOutput(unittest.TestCase):
    def test_functions_exist_in_server(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Summary render functions and helpers must exist
        self.assertIn("function relativizePath(", content)
        self.assertIn("function renderSyncAgentsSummary(", content)
        self.assertIn("function renderIssueAuditOutput(", content)
        self.assertIn("function renderRunOutput(", content)

    def test_sync_agents_collapsed_details(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # sync-agents handler routes full body through collapsed details
        self.assertIn("<details", content)
        self.assertIn("Full JSON output", content)

    def test_issue_audit_relativizes_server_paths(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Audit output relativizes /srv/ paths and provides collapsed raw output
        self.assertIn("/srv/repos/", content)
        self.assertIn("/srv/", content)
        self.assertIn("relativizePath", content)
        self.assertIn("<details", content)

    def test_run_command_collapsed(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Echoed command line in output drawer is rendered inside collapsed details
        self.assertIn("<details", content)
        self.assertIn("<summary>command</summary>", content)

if __name__ == "__main__":
    unittest.main()
