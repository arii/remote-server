"""Grep-guard: deep-agent runtime state stays out of the buttons
build context (#562). Removing any listed line from .dockerignore
fails this test (and re-bloats the image with dead state).
"""
import unittest

# Every known deep-agent runtime-state path must stay dockerignored.
REQUIRED_IGNORES = [
    "deep-agent/chroma_db/",
    "deep-agent/.chat_threads/",
    "deep-agent/traces/",
    "deep-agent/ledger.jsonl",
    "deep-agent/.venv/",
    "deep-agent/tests/",
]


def _ignore_lines():
    with open(".dockerignore") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


class TestDockerignore(unittest.TestCase):
    def test_runtime_state_ignored(self):
        lines = _ignore_lines()
        for required in REQUIRED_IGNORES:
            self.assertIn(
                required, lines,
                f"{required} missing from .dockerignore")

    def test_guard_is_meaningful(self):
        # The guard only matters because the image COPYs the tree:
        # without the exclusion lines, COPY would bake the state in.
        with open("Dockerfile") as f:
            body = f.read()
        self.assertIn("COPY deep-agent/", body)


if __name__ == "__main__":
    unittest.main()
