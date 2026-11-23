"""Dependency removal guard (#1535): dropped packages stay unimported and
test-only packages stay out of runtime code and runtime requirements.

DROPPED were removed from deep-agent/requirements.txt after a repo-wide
check found no import, dynamic import or Dockerfile use. TEST_ONLY moved
to backend/requirements-test.txt; runtime code must not need them, since
the runtime image installs only the two runtime requirements files.
Run: python3 -m unittest tests.test_unused_deps"""

import ast
import os
import subprocess
import unittest

DROPPED = {
    "langchain_chroma": "langchain-chroma",
    "langchain_text_splitters": "langchain-text-splitters",
}
TEST_ONLY = {"yaml": "PyYAML", "jsonschema": "jsonschema"}
RUNTIME_REQS = ("backend/requirements.txt", "deep-agent/requirements.txt")
DYNAMIC = {"import_module", "__import__"}


def _root():
    return os.getcwd()


def _tracked_py():
    out = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=_root(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.splitlines() if os.path.exists(os.path.join(_root(), p))]


def _is_test(path):
    parts = path.split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_")


def _imported(path):
    """Top-level module names imported by path, statically or by a literal
    importlib.import_module / __import__ call."""
    with open(os.path.join(_root(), path), encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and node.args:
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            arg = node.args[0]
            if (
                called in DYNAMIC
                and isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
            ):
                names.add(arg.value.split(".")[0])
    return names


def _req_names(path):
    with open(os.path.join(_root(), path), encoding="utf-8") as f:
        lines = [ln.split("#")[0].strip() for ln in f]
    return {ln.split("==")[0].strip().lower() for ln in lines if ln}


class TestUnusedDeps(unittest.TestCase):
    def test_dropped_modules_not_imported_anywhere(self):
        for path in _tracked_py():
            if path == "tests/test_unused_deps.py":
                continue
            hit = _imported(path) & set(DROPPED)
            self.assertFalse(hit, f"{path} imports dropped module(s) {sorted(hit)}")

    def test_test_only_modules_not_imported_by_runtime_code(self):
        runtime = [p for p in _tracked_py() if not _is_test(p)]
        self.assertTrue(runtime, "no runtime python files found; wrong cwd?")
        for path in runtime:
            hit = _imported(path) & set(TEST_ONLY)
            self.assertFalse(
                hit,
                f"{path} imports test-only module(s) {sorted(hit)}; "
                "move the pin back to backend/requirements.txt",
            )

    def test_runtime_requirements_exclude_dropped_and_test_only(self):
        banned = {v.lower() for v in (*DROPPED.values(), *TEST_ONLY.values())}
        for req in RUNTIME_REQS:
            self.assertFalse(
                _req_names(req) & banned, f"{req} lists a dropped/test-only pin"
            )

    def test_test_requirements_carry_test_only_pins(self):
        self.assertEqual(
            _req_names("backend/requirements-test.txt"),
            {v.lower() for v in TEST_ONLY.values()},
        )


if __name__ == "__main__":
    unittest.main()
