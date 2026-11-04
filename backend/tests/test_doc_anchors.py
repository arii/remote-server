"""docs/agent-integration-guide.md symbol anchors stay true (#545).

The guide cites backend/ and deep-agent/ code by symbol (module +
function/route/constant name), never by line number. This suite fails
when a cited symbol disappears, so rot is caught at the landing that
causes it:

- no bare file:line pins remain in the guide (symbols only);
- every cited symbol still exists in its cited file;
- the citation table cannot drift from the guide (each entry's token
  must appear in the guide text).

Symbols the guide names as removed (run_pi_pipeline, the ask/suggest
endpoints, buttons/server.py contents) are deliberately NOT tabled:
the table covers live anchors only.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUIDE = REPO_ROOT / "docs" / "agent-integration-guide.md"

# (cited file, needle grepped in that file, token named in the guide).
# Needles carry `def `/`class ` context so short names (health, main)
# cannot match an unrelated line.
ANCHORS = [
    ("backend/server.py", "include_router", "include_router"),
    ("backend/api/routers/agents.py", "def health(", "health"),
    ("backend/api/routers/agents.py", "def agent_runs(", "agent_runs"),
    ("backend/api/routers/agents.py", "def agent_run(", "agent_run"),
    ("deep-agent/runs.py", "def list_runs(", "list_runs"),
    ("deep-agent/runs.py", "def get_run(", "get_run"),
    ("backend/api/routers/chat.py", "def chat_stream_get(", "chat_stream_get"),
    ("backend/api/routers/chat.py", "def chat_stream_post(", "chat_stream_post"),
    ("backend/api/routers/chat.py", "def chat_abort(", "chat_abort"),
    ("backend/api/routers/chat.py", "def pi_progress(", "pi_progress"),
    ("backend/api/routers/chat.py", "def _chat_events(", "_chat_events"),
    ("backend/exec/engine.py", "class RunRegistry", "RunRegistry"),
    ("backend/api/routers/ops.py", "def workflow_health(", "workflow_health"),
    (
        "backend/api/routers/ops.py",
        "def fetch_workflow_health(",
        "fetch_workflow_health",
    ),
    ("deep-agent/deep_chat.py", "def main(", "main"),
    ("deep-agent/deep_chat.py", "def validate_proposal(", "validate_proposal"),
    ("deep-agent/deep_chat.py", "EXIT_NEEDS_APPROVAL", "EXIT_NEEDS_APPROVAL"),
    ("deep-agent/observe.py", "def default_trace_path(", "default_trace_path"),
    ("deep-agent/observe.py", "class TraceRecorder", "TraceRecorder"),
    ("deep-agent/ask_investigator.py", "def pr_state(", "pr_state"),
]

# Bare file:line pins: path.py:123. Symbol anchors (deep_chat.py:main)
# have no digits after the colon and never match.
FILE_LINE_PIN = re.compile(r"\b[A-Za-z0-9_./-]+\.(?:py|tsx?|jsx?|sh|yml|yaml|json|md|css):\d")

# Bare section-relative pins: `:7309`, `:13-24`. The lookbehind keeps
# `::` ranges and `INTERRUPT: {...}` prose out of the match.
BARE_LINE_PIN = re.compile(r"(?<![\w/.:])\:\d{2,}")


def assert_anchor(path, needle):
    """Fail unless needle still exists in the cited file."""
    text = (REPO_ROOT / path).read_text()
    if needle not in text:
        raise AssertionError(f"rotted anchor: {needle!r} not found in {path}")


class DocAnchorTests(unittest.TestCase):
    def test_no_file_line_pins(self):
        guide = GUIDE.read_text()
        pins = FILE_LINE_PIN.findall(guide) + BARE_LINE_PIN.findall(guide)
        self.assertEqual(pins, [], f"bare file:line pins remain: {pins}")

    def test_cited_symbols_exist(self):
        for path, needle, _token in ANCHORS:
            with self.subTest(path=path, needle=needle):
                assert_anchor(path, needle)

    def test_table_matches_guide(self):
        guide = GUIDE.read_text()
        for _path, _needle, token in ANCHORS:
            with self.subTest(token=token):
                self.assertIn(token, guide, f"{token!r} no longer named in the guide")

    def test_missing_symbol_fails(self):
        """Negative control: a disappeared symbol fails the check."""
        with self.assertRaises(AssertionError):
            assert_anchor("backend/api/routers/chat.py", "def no_such_symbol_xyz(")


if __name__ == "__main__":
    unittest.main()
