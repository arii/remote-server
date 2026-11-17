import asyncio
from pathlib import Path
import tempfile
import unittest

import coder

try:
    import deepagents  # noqa: F401

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderBackend(unittest.TestCase):
    def test_outside_absolute_paths_return_root_error(self):
        with tempfile.TemporaryDirectory() as root:
            backend = coder.build_filesystem_backend(root)
            for result in (
                backend.ls("/etc"),
                backend.read("/etc/passwd"),
                backend.write("/outside-probe", "blocked"),
                backend.edit("/etc/passwd", "absent", "blocked"),
                backend.glob("*", path="/etc"),
                backend.grep("x", path="/etc"),
                backend.ls(root),
            ):
                with self.subTest(result=result):
                    self.assertIn("outside_worktree_root", result.error)
                    self.assertIn("worktree-relative", result.error)

    def test_relative_empty_missing_and_normal_paths(self):
        with tempfile.TemporaryDirectory() as root:
            backend = coder.build_filesystem_backend(root)
            self.assertEqual(backend.ls(".").entries, [])
            self.assertIn("path_not_found", backend.ls("missing").error)
            self.assertIsNone(backend.write("sub/a.txt", "hello").error)
            self.assertEqual(Path(root, "sub/a.txt").read_text(), "hello")
            self.assertIsNone(backend.read("sub/a.txt").error)
            self.assertIsNone(backend.ls("/").error)

    def test_traversal_and_symlink_escape(self):
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as outside,
        ):
            Path(root, "link").symlink_to(outside, target_is_directory=True)
            backend = coder.build_filesystem_backend(root)
            for path in ("../outside", "link/secret"):
                self.assertIn("outside_worktree_root", backend.read(path).error)

    def test_async_write_and_boundary_use_same_backend(self):
        with tempfile.TemporaryDirectory() as root:
            written = set()
            backend = coder.build_filesystem_backend(root, written)
            self.assertIsNone(asyncio.run(backend.awrite("ok.txt", "ok")).error)
            self.assertEqual(written, {"ok.txt"})
            self.assertIn(
                "outside_worktree_root", asyncio.run(backend.aread("/etc/passwd")).error
            )
