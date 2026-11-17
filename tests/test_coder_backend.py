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
    def tool_call(self, root, name, args):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        model = LocalModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": name,
                            "args": args,
                            "id": "probe",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="finished"),
            ]
        )
        agent = coder.build_agent(root, "test", model=model)
        result = agent.invoke({"messages": [HumanMessage(content="probe")]})
        return next(message for message in result["messages"] if message.type == "tool")

    def test_outside_absolute_paths_return_root_error(self):
        with tempfile.TemporaryDirectory() as root:
            for name, args in (
                ("ls", {"path": "/etc"}),
                ("read_file", {"file_path": "/etc/passwd"}),
                ("write_file", {"file_path": "/outside-probe", "content": "blocked"}),
                (
                    "edit_file",
                    {
                        "file_path": "/etc/passwd",
                        "old_string": "absent",
                        "new_string": "blocked",
                    },
                ),
                ("glob", {"pattern": "*", "path": "/etc"}),
                ("grep", {"pattern": "x", "path": "/etc"}),
                ("ls", {"path": root}),
            ):
                with self.subTest(name=name):
                    result = self.tool_call(root, name, args)
                    self.assertEqual(result.status, "error")
                    self.assertIn("outside_worktree_root", result.content)
                    self.assertIn("worktree-relative", result.content)

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
            backend = coder.build_filesystem_backend(root)
            self.assertIsNone(asyncio.run(backend.awrite("ok.txt", "ok")).error)
            self.assertEqual(Path(root, "ok.txt").read_text(), "ok")
            self.assertIn(
                "outside_worktree_root", asyncio.run(backend.aread("../outside")).error
            )
