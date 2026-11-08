"""repair_worker fetch-corruption attribution (audit fallback batch).

parse_issue_node must raise a RuntimeError naming fetch corruption —
never a spec-parse/unscaffolded error — when `gh issue view` exits 0
with non-JSON stdout. gh is stubbed at backend.repair_worker.subprocess
so the test never touches the network.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import repair_worker as repair_worker_mod


class FetchCorruptionTest(unittest.TestCase):
    def test_non_json_stdout_raises_fetch_corruption(self):
        proc = SimpleNamespace(returncode=0, stdout="this is not json{{{", stderr="")
        with patch("backend.repair_worker.subprocess") as subprocess_mock:
            subprocess_mock.run.return_value = proc
            with self.assertRaises(RuntimeError) as ctx:
                repair_worker_mod.parse_issue_node(
                    {"issue_number": 9, "issue_body": "", "repo": "remote-server"}
                )
        subprocess_mock.run.assert_called_once()
        exc = ctx.exception
        self.assertIn("fetch corruption", str(exc))
        self.assertIn("not a spec error", str(exc))
        self.assertNotIsInstance(exc, repair_worker_mod.UnscaffoldedIssueError)


if __name__ == "__main__":
    unittest.main()
