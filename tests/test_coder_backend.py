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
    def test_1570_recorded_inspections_replay_to_first_edit(self):
        """Replay calls from fa2e25a6cb54, then simulate the prompted correction."""
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        bindings = []

        class Model(LocalModel):
            def bind_tools(self, tools, **kwargs):
                bindings.append(
                    ({tool.name for tool in tools}, kwargs.get("tool_choice"))
                )
                return self

        def call(id, name, **args):
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": id, "name": name, "args": args, "type": "tool_call"}
                ],
            )

        with tempfile.TemporaryDirectory() as root:
            checkout = Path(root)
            (checkout / "backend").mkdir()
            (checkout / "docs/specs").mkdir(parents=True)
            spec = "docs/specs/issue-1570.md"
            (checkout / spec).write_text("Recorded spec\n")
            path = "backend/proposals.py"
            (checkout / path).write_text("a = 1\nb = 1\n")
            (checkout / "backend/tests").mkdir()
            (checkout / "backend/tests/test_jobs_exec.py").touch()
            recorded_plan = (
                f"1. Draft [files: {spec}]\n2. Add [files: backend/models/runner.py]"
            )
            self.assertEqual(
                coder.first_edit_targets(root, recorded_plan, exclude=(spec,)), {}
            )
            targets = coder.first_edit_targets(
                root, recorded_plan + f"\n3. Update [files: {path}]", exclude=(spec,)
            )
            self.assertEqual(targets, {path: True})
            model = Model(
                responses=[
                    call("7a8e687c", "ls", path="."),
                    call("e2d6c7eb", "ls", path="backend/models"),
                    call("167f9d7e", "read_file", file_path=f"/{spec}", limit=200),
                    call("fa37f305", "read_file", file_path=f"/{path}", limit=50),
                    call(
                        "94601863",
                        "read_file",
                        file_path="/backend/tests/test_jobs_exec.py",
                        limit=50,
                    ),
                    call(
                        "missing",
                        "edit_file",
                        file_path=path,
                        old_string="bad",
                        new_string="new",
                    ),
                    call(
                        "ambiguous",
                        "edit_file",
                        file_path=path,
                        old_string="1",
                        new_string="2",
                    ),
                    call(
                        "applied",
                        "edit_file",
                        file_path=path,
                        old_string="a = 1",
                        new_string="a = 2",
                    ),
                    AIMessage(content="DONE #1570"),
                ]
            )
            written = coder.WriteTracker()
            graph = coder.build_agent(
                root,
                coder.build_system(root, ""),
                model=model,
                written_files=written,
                first_edit=targets,
            )
            result = graph.invoke({"messages": [HumanMessage(content="Fix #1570")]})
            self.assertEqual((checkout / path).read_text(), "a = 2\nb = 1\n")
            self.assertEqual(written, {path})
            self.assertEqual(written.mutations, 1)
            self.assertEqual(bindings[:4], [({"read_file"}, "required")] * 4)
            self.assertEqual(bindings[4:8], [({"edit_file"}, "required")] * 4)
            self.assertIn("run_check", bindings[8][0])
            tools = {m.tool_call_id: m for m in result["messages"] if m.type == "tool"}
            self.assertIn("Read each planned file", tools["7a8e687c"].content)
            self.assertIn("Make the first planned edit now", tools["94601863"].content)
            for id in ("missing", "ambiguous"):
                self.assertEqual(tools[id].status, "error")
                self.assertIn("match exactly once", tools[id].content)
            self.assertEqual(tools["applied"].status, "success")

    def test_implementation_requires_tool_call_until_successful_grounding(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        choices = []

        class Model(LocalModel):
            def bind_tools(self, tools, **kwargs):
                choices.append(kwargs.get("tool_choice"))
                return self

        with tempfile.TemporaryDirectory() as root:
            model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {"file_path": "/etc/passwd"},
                                "id": "outside",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ls",
                                "args": {"path": "."},
                                "id": "inside",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    AIMessage(content="BLOCKED #1"),
                ]
            )
            agent = coder.build_agent(
                root, coder.build_system(root, ""), model=model, written_files=set()
            )
            result = agent.invoke(
                {"messages": [HumanMessage(content="Inspect repository")]}
            )
            self.assertEqual(choices, ["required", "required", None])
            self.assertEqual(result["messages"][-1].content, "BLOCKED #1")

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
