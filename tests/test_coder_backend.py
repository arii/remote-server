import asyncio
import ast
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
    def test_host_absolute_paths_inside_worktree_are_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "new.py")
            written = set()
            write = self.tool_call(
                root,
                "write_file",
                {"file_path": str(target), "content": "inside"},
                written,
            )
            self.assertEqual(write.status, "success", write.content)
            self.assertEqual(target.read_text(), "inside")
            self.assertEqual(written, {"new.py"})
            read = self.tool_call(root, "read_file", {"file_path": str(target)})
            self.assertEqual(read.status, "success", read.content)
            self.assertIn("inside", read.content)
            self.assertEqual(
                self.tool_call(root, "ls", {"path": root}).status, "success"
            )
            backend = coder.build_filesystem_backend(root)
            self.assertIsNone(backend.read(str(target)).error)
            self.assertIsNone(asyncio.run(backend.aread(str(target))).error)

    def test_verification_tool_executes_in_worktree_without_dead_execute(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        bound = []

        class Model(LocalModel):
            def bind_tools(self, tools, **kwargs):
                bound.extend(t.name for t in tools)
                return self

        with tempfile.TemporaryDirectory() as root:
            Path(root, "test_location.py").write_text(
                "from pathlib import Path\ndef test_location():\n"
                "    assert Path('worktree-marker').read_text() == 'inside'\n"
            )
            Path(root, "worktree-marker").write_text("inside")
            model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "run_check",
                                "args": {
                                    "command": "python3 -m pytest -q test_location.py"
                                },
                                "id": "verify",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="done"),
                ]
            )
            agent = coder.build_agent(root, "Verify using run_check", model=model)
            self.assertNotIn(
                "execute", agent.get_graph().nodes["tools"].data.tools_by_name
            )
            result = agent.invoke({"messages": [HumanMessage(content="verify")]})
            tool = next(m for m in result["messages"] if m.type == "tool")
            self.assertIn("exit=0", tool.content)
            self.assertIn("1 passed", tool.content)
            self.assertIn("run_check", bound)
            self.assertNotIn("execute", bound)

    def test_discovered_virtual_paths_can_be_read_and_edited(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "src").mkdir()
            target = Path(root, "src/app.py")
            for discovery, args in (
                ("ls", {"path": "src"}),
                ("glob", {"pattern": "**/*.py"}),
            ):
                with self.subTest(discovery=discovery):
                    target.write_text("before")
                    result = self.tool_call(root, discovery, args)
                    (path,) = ast.literal_eval(result.content)
                    self.assertEqual(path, "/src/app.py")
                    read = self.tool_call(root, "read_file", {"file_path": path})
                    self.assertEqual(read.status, "success", read.content)
                    self.assertIn("before", read.content)
                    edit = self.tool_call(
                        root,
                        "edit_file",
                        {
                            "file_path": path,
                            "old_string": "before",
                            "new_string": "after",
                        },
                    )
                    self.assertEqual(edit.status, "success", edit.content)
                    self.assertEqual(target.read_text(), "after")

    def test_tool_paths_reject_traversal_and_symlink_escapes(self):
        with (
            tempfile.TemporaryDirectory() as root,
            tempfile.TemporaryDirectory() as outside,
        ):
            Path(outside, "secret").write_text("private")
            Path(root, "link").symlink_to(outside, target_is_directory=True)
            for path in (
                "../",
                "/../etc/passwd",
                "/link/secret",
                "link/secret",
                "/etc/passwd",
                str(Path(root, "link/secret")),
                str(Path(root, "../outside")),
            ):
                with self.subTest(path=path):
                    result = self.tool_call(root, "read_file", {"file_path": path})
                    self.assertEqual(result.status, "error")
                    self.assertIn("outside_worktree_root", result.content)
                    self.assertNotIn("private", result.content)

    def tool_call(self, root, name, args, written_files=None):
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
        agent = coder.build_agent(
            root, "test", model=model, written_files=written_files
        )
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
                ("ls", {"path": root + "-outside"}),
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
