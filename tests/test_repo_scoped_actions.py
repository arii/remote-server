#!/usr/bin/env python3
"""Repo-scoped actions must follow the picker on both sides.

Regression: merge-approved ran against tech-dancer regardless of the
picked repo because the client only forwarded the picker value for a
hardcoded subset of REPO_SCOPED_ACTIONS (observed live: a
remote-server run executed merge_approved.py arii/tech-dancer).
"""

import re
import unittest

import buttons.server as srv


class TestRepoScopedActions(unittest.TestCase):
    def test_merge_approved_registered(self):
        self.assertIn("merge-approved", srv.ACTIONS)
        self.assertIn("merge-approved", srv.REPO_SCOPED_ACTIONS)
        spec = srv.ACTIONS["merge-approved"]
        self.assertEqual(spec["group"], "Briefing")
        self.assertTrue(spec["confirm"])

    def test_client_forwards_picker_for_all_repo_scoped(self):
        with open("buttons/server.py", encoding="utf-8") as f:
            src = f.read()
        m = re.search(
            r'if \(name === .*?\) \{\n'
            r'    const picker = document.getElementById\("briefingRepo"\);',
            src,
        )
        self.assertIsNotNone(m, "client picker-forwarding condition not found")
        cond = m.group(0)
        for action in sorted(srv.REPO_SCOPED_ACTIONS):
            self.assertIn(f'name === "{action}"', cond,
                          f"client drops picker repo for {action}")


if __name__ == "__main__":
    unittest.main()
