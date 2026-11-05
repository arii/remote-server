"""No-shadow-cli guards: core_cli/ must stay deleted and deterministic
tooling must route through backend.core.run_td (td --json).

Locks the follow_up_guide section 3 end-state: the top-level core_cli/
package is gone, no caller references it (or run_core_cli) in backend/,
deep-agent/, tests/, backend/tests/, .github/, Makefile or mk/, and
backend.core exposes run_td as the single deterministic entry point.
Run: python3 -m unittest tests.test_no_core_cli"""
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

SCOPES = ("backend", "deep-agent", "tests", "backend/tests", ".github", "Makefile", "mk")

SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git", "chroma_db"}

SELF = os.path.basename(__file__)


def iter_files(scope):
    path = os.path.join(ROOT, scope)
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield os.path.join(dirpath, name)


class TestNoCoreCli(unittest.TestCase):
    def test_core_cli_directory_absent(self):
        """core_cli/ must hold no source (stale __pycache__ residue excepted)."""
        pkg = os.path.join(ROOT, "core_cli")
        sources = []
        if os.path.isdir(pkg):
            for dirpath, dirnames, filenames in os.walk(pkg):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                sources += [f for f in filenames if f.endswith((".py", ".js"))]
        self.assertEqual(sources, [], f"core_cli/ sources are back: {sources}")

    def test_zero_core_cli_references(self):
        """No source, test, workflow or makefile may reference core_cli."""
        hits = []
        for scope in SCOPES:
            for path in iter_files(scope):
                if os.path.basename(path) == SELF:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="strict") as handle:
                        text = handle.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if "core_cli" in text:
                    hits.append(os.path.relpath(path, ROOT))
        self.assertEqual(hits, [], f"core_cli references found: {hits}")

    def test_zero_run_core_cli_references(self):
        """run_core_cli must not exist anywhere; run_td is the entry point."""
        hits = []
        for scope in SCOPES:
            for path in iter_files(scope):
                if os.path.basename(path) == SELF:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="strict") as handle:
                        text = handle.read()
                except (OSError, UnicodeDecodeError):
                    continue
                if "run_core_cli" in text:
                    hits.append(os.path.relpath(path, ROOT))
        self.assertEqual(hits, [], f"run_core_cli references found: {hits}")

    def test_run_td_entry_point(self):
        """backend.core must expose run_td as the single deterministic gateway."""
        import backend.core as core

        self.assertTrue(callable(getattr(core, "run_td", None)), "backend.core.run_td missing")
        self.assertFalse(hasattr(core, "run_core_cli"), "backend.core.run_core_cli must stay deleted")


if __name__ == "__main__":
    unittest.main()
