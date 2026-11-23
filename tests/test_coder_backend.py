import asyncio
import ast
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import coder

try:
    import deepagents  # noqa: F401

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderBackend(unittest.TestCase):
    def run_completion(self, root, steps, rounds):
        import deep_chat
        import observe
        from backend.shared import ledger
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from tests.test_coder_outcome import LocalModel

        prompts = []

        class Model(LocalModel):
            def _generate(self, messages, *args, **kwargs):
                if messages[-1].type == "human":
                    prompts.append(messages[-1].content)
                return super()._generate(messages, *args, **kwargs)

        replies = []
        for i, step in enumerate(steps):
            if isinstance(step, str):
                replies.append(AIMessage(content=step))
            else:
                name, args = step
                replies.append(
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": args,
                                "id": str(i),
                                "type": "tool_call",
                            }
                        ],
                    )
                )
        with (
            mock.patch.object(
                coder,
                "build_model",
                return_value=(
                    Model(responses=replies),
                    {"provider": "twt", "model": "test"},
                ),
            ),
            mock.patch.object(coder, "verify_rounds", return_value=rounds),
            mock.patch.object(
                deep_chat,
                "open_checkpointer",
                return_value=(InMemorySaver(), False, None),
            ),
            mock.patch.object(
                observe,
                "default_trace_path",
                return_value=str(Path(root, "trace.jsonl")),
            ),
            mock.patch.object(ledger, "record"),
        ):
            result = coder.run_coder(root, "Implement #1570", "system", "thread", 10)
        return result, prompts

    def test_failed_self_check_is_sent_back_for_a_fix(self):
        path = "tests/test_runner_status.py"
        bad_import = "from nonexistent_runner import runner_state"
        test = "import unittest\nclass TestRunner(unittest.TestCase):\n    def test_state(self):\n        self.assertEqual(runner_state, 'idle')\n"
        command = "python3 -m unittest tests.test_runner_status"
        check = ("run_check", {"command": command})
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "tests").mkdir()
            (Path(root) / "tests/__init__.py").touch()
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "baseline",
                    "--allow-empty",
                ],
                check=True,
            )
            result, prompts = self.run_completion(
                root,
                [
                    (
                        "write_file",
                        {"file_path": path, "content": bad_import + "\n" + test},
                    ),
                    "Wrote the test.\nDONE #1570",
                    check,
                    "DONE #1570",
                    (
                        "edit_file",
                        {
                            "file_path": path,
                            "old_string": bad_import,
                            "new_string": "runner_state = 'idle'",
                        },
                    ),
                    check,
                    "Fixed the import.\nDONE #1570",
                ],
                rounds=2,
            )
            self.assertNotIn(bad_import, Path(root, path).read_text())
        self.assertEqual(len(prompts), 3)
        self.assertEqual(prompts[1], coder.VERIFY_NUDGE)
        self.assertIn(
            f"Latest failed run_check: {command}",
            prompts[2],
        )
        self.assertIn(
            "ModuleNotFoundError: No module named 'nonexistent_runner'", prompts[2]
        )
        self.assertTrue(result[6])
        self.assertEqual(result[7], 2)

    def test_missing_terminal_line_asks_once_and_uses_reply(self):
        with tempfile.TemporaryDirectory() as root:
            result, prompts = self.run_completion(
                root,
                [
                    ("write_file", {"file_path": "result.txt", "content": "done"}),
                    "Changed result.txt.",
                    "Changed result.txt.\nDONE #1570",
                ],
                rounds=0,
            )
        self.assertEqual(prompts, ["Implement #1570", coder.TERMINAL_NUDGE])
        self.assertEqual(result[0], "Changed result.txt.\nDONE #1570")
        self.assertFalse(result[6])

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
            self.assertEqual(
                bindings[4:8], [({"edit_file", "replace_lines"}, "required")] * 4
            )
            self.assertIn("run_check", bindings[8][0])
            tools = {m.tool_call_id: m for m in result["messages"] if m.type == "tool"}
            self.assertIn("Read each planned file", tools["7a8e687c"].content)
            self.assertIn("Make the first planned edit now", tools["94601863"].content)
            for id in ("missing", "ambiguous"):
                self.assertEqual(tools[id].status, "error")
                self.assertIn("match exactly once", tools[id].content)
            self.assertEqual(tools["applied"].status, "success")

    def test_1570_missing_docstring_quotes_replay(self):
        from deepagents.middleware.filesystem import FilesystemMiddleware
        from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
        from langchain_core.messages import ToolMessage

        path = "backend/server.py"
        first_line = '"""Minimal FastAPI application entrypoint."""'
        original = first_line + "\n\nimport os\nimport sys\n"
        bad = {
            "file_path": path,
            "old_string": first_line[:-2] + "\n\nimport os\nimport sys",
            "new_string": first_line[:-2]
            + "\n\nimport os\nimport sys\nfrom enum import Enum",
        }

        for repeats in (False, True):
            with self.subTest(repeats=repeats), tempfile.TemporaryDirectory() as root:
                target = Path(root, path)
                target.parent.mkdir()
                target.write_text(original)
                written = coder.WriteTracker()
                with mock.patch("deepagents.create_deep_agent") as create:
                    coder.build_agent(
                        root,
                        "",
                        model=mock.Mock(),
                        written_files=written,
                        first_edit={path: True},
                    )
                config = create.call_args.kwargs
                tools = {tool.name: tool for tool in config["tools"]}

                def run(request):
                    if request.tool is None:
                        return ToolMessage(
                            content=target.read_text(),
                            tool_call_id=request.tool_call["id"],
                        )
                    return request.tool.invoke(request.tool_call)

                def wrap(layer, handler):
                    return lambda request: layer.wrap_tool_call(request, handler)

                for layer in reversed(config["middleware"]):
                    if type(
                        layer
                    ).wrap_tool_call is AgentMiddleware.wrap_tool_call or isinstance(
                        layer, FilesystemMiddleware
                    ):
                        continue
                    run = wrap(layer, run)

                def call(id, name, **args):
                    return run(
                        ToolCallRequest(
                            tool_call={
                                "id": id,
                                "name": name,
                                "args": args,
                                "type": "tool_call",
                            },
                            tool=tools.get(name),
                            state={},
                            runtime=None,
                        )
                    )

                self.assertEqual(
                    call("read", "read_file", file_path=path).status, "success"
                )
                mismatch = call("64b57473", "edit_file", **bad)
                duplicate = call("26413794", "edit_file", **bad)
                if repeats:
                    with self.assertRaisesRegex(
                        coder.CoderBudgetExceeded,
                        "repeated unchanged tool call: edit_file",
                    ):
                        call("1d714dd5", "edit_file", **bad)
                    self.assertEqual(target.read_text(), original)
                    self.assertEqual(written.mutations, 0)
                else:
                    corrected = call(
                        "corrected",
                        "replace_lines",
                        file_path=path,
                        start_line=4,
                        end_line=4,
                        new_text="import sys\nfrom enum import Enum",
                    )
                    self.assertEqual(corrected.status, "success")
                    self.assertEqual(
                        target.read_text(), original + "from enum import Enum\n"
                    )
                    self.assertEqual(written, {path})
                    self.assertEqual(written.mutations, 1)
                self.assertEqual(mismatch.status, "error")
                self.assertIn("Do not resend the same arguments", mismatch.content)
                self.assertIn(
                    f"Closest region:\n1 | {first_line}\n2 | \n3 | import os",
                    mismatch.content,
                )
                self.assertEqual(duplicate.status, "error")
                self.assertIn("use replace_lines instead", duplicate.content)

    def test_line_replacement_validates_scope_and_preserves_whole_lines(self):
        with tempfile.TemporaryDirectory() as root:
            planned = Path(root, "planned.py")
            unplanned = Path(root, "unplanned.py")
            original = b"first\r\nsecond\r\nthird"
            planned.write_bytes(original)
            unplanned.write_bytes(original)
            tool = coder.build_replace_lines_tool(
                coder.build_filesystem_backend(root), {"planned.py": True}
            )

            def replace(path, start, end, text):
                return tool.invoke(
                    {
                        "name": "replace_lines",
                        "id": "replace",
                        "type": "tool_call",
                        "args": dict(
                            file_path=path,
                            start_line=start,
                            end_line=end,
                            new_text=text,
                        ),
                    }
                )

            for start, end in [(0, 1), (3, 2), (2, 4)]:
                self.assertEqual(
                    replace("planned.py", start, end, "no").status, "error"
                )
            self.assertEqual(replace("unplanned.py", 1, 1, "no").status, "error")
            self.assertEqual(planned.read_bytes(), original)
            self.assertEqual(unplanned.read_bytes(), original)
            self.assertEqual(replace("planned.py", 2, 2, "new\nline").status, "success")
            self.assertEqual(planned.read_bytes(), b"first\r\nnew\r\nline\r\nthird")
            self.assertEqual(replace("planned.py", 2, 3, "").status, "success")
            self.assertEqual(planned.read_bytes(), b"first\r\nthird")
            self.assertEqual(
                replace("planned.py", 2, 3, "").artifact, coder.ALREADY_APPLIED
            )
            self.assertEqual(
                replace("planned.py", 2, 2, "third").artifact, coder.ALREADY_APPLIED
            )
            self.assertEqual(planned.read_bytes(), b"first\r\nthird")

    def test_1570_applied_docstring_replay_keeps_file_and_repeat_guard(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        file_path = "backend/notify/prefs_state.py"
        old = '"""Operator prefs/state + notification send gate."""'
        new = (
            '"""Operator prefs/state + notification send gate.\n\n'
            "The notification dispatch gate determines a runner’s health.\n"
            '"""'
        )
        for name, edit_args in (
            ("edit_file", {"old_string": old, "new_string": new}),
            ("replace_lines", {"start_line": 1, "end_line": 1, "new_text": new}),
        ):
            for stop in (False, True):
                with (
                    self.subTest(name=name, stop=stop),
                    tempfile.TemporaryDirectory() as root,
                ):
                    target = Path(root, file_path)
                    target.parent.mkdir(parents=True)
                    target.write_text(f"{old}\n\nVALUE = 1\n")

                    def call(id, tool, **args):
                        return AIMessage(
                            content="",
                            tool_calls=[
                                dict(
                                    id=id,
                                    name=tool,
                                    args={"file_path": file_path, **args},
                                    type="tool_call",
                                )
                            ],
                        )

                    model = LocalModel(
                        responses=[
                            call("read", "read_file"),
                            call("51a2c30c", name, **edit_args),
                            call("345db283", name, **edit_args),
                            call("89afe4dc", name, **edit_args),
                            call("stop", name, **edit_args)
                            if stop
                            else AIMessage(content="Done"),
                        ]
                    )
                    written = coder.WriteTracker()
                    agent = coder.build_agent(
                        root,
                        "test",
                        model=model,
                        written_files=written,
                        first_edit={file_path: True},
                    )
                    request = {
                        "messages": [HumanMessage(content="Update the docstring")]
                    }
                    if stop:
                        with self.assertRaisesRegex(
                            coder.CoderBudgetExceeded,
                            f"repeated unchanged tool call: {name}",
                        ):
                            agent.invoke(request)
                    else:
                        result = agent.invoke(request)
                        replies = {
                            m.tool_call_id: m
                            for m in result["messages"]
                            if m.type == "tool"
                        }
                        applied = replies["51a2c30c"]
                        self.assertEqual(applied.status, "success")
                        self.assertIn(
                            "This change is now in the file.", applied.content
                        )
                        self.assertIn(
                            "3 | The notification dispatch gate", applied.content
                        )
                        replay = replies["345db283"]
                        self.assertEqual(replay.status, "success")
                        self.assertIn(
                            "already applied; don't resend it, move to the next step",
                            replay.content,
                        )
                        self.assertEqual(replies["89afe4dc"].status, "error")
                    self.assertEqual(target.read_text(), f"{new}\n\nVALUE = 1\n")
                    self.assertEqual(written.mutations, 1)

    def test_edit_replay_requires_unique_new_text_and_no_old_text(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, "state.txt")
            target.write_text("new\nnew\n")
            reply = self.tool_call(
                root,
                "edit_file",
                {"file_path": "state.txt", "old_string": "old", "new_string": "new"},
            )
            self.assertEqual(reply.status, "error")
            target.write_text("old\nnew\n")
            written = coder.WriteTracker()
            reply = self.tool_call(
                root,
                "edit_file",
                {"file_path": "state.txt", "old_string": "old", "new_string": "new"},
                written,
            )
            self.assertIn("Updated region:\n1 | new", reply.content)
            self.assertEqual(reply.status, "success")
            self.assertEqual(target.read_text(), "new\nnew\n")
            self.assertEqual(written.mutations, 1)

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

    def test_viewer_repair_rejects_invalid_edits_before_writing(self):
        file_path = "backend/tests/test_viewer_empty_state.py"
        docstring = '"""Viewer status row + empty state.\n\nThe live pill clipped.\n"""'
        original = docstring + "\n\nimport unittest\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, file_path)
            path.parent.mkdir(parents=True)
            path.write_text(original)
            backend = coder.build_filesystem_backend(root)
            replace_lines = coder.build_replace_lines_tool(backend, {file_path: True})
            edit_file = coder.build_edit_tool(backend)
            escaped = docstring.replace("\n", r"\n") + r"\n\n# Import for test coverage"
            cases = (
                (
                    replace_lines,
                    dict(
                        file_path=file_path, start_line=1, end_line=4, new_text=escaped
                    ),
                    "unexpected character after line continuation character",
                    f"1 | {escaped}",
                ),
                (
                    edit_file,
                    dict(
                        file_path=file_path,
                        old_string="import unittest",
                        new_string='"""',
                    ),
                    "unterminated triple-quoted string literal",
                    '6 | """',
                ),
            )
            for tool, args, error, excerpt in cases:
                with self.subTest(tool=tool.name):
                    result = tool.invoke(
                        dict(name=tool.name, id="repair", type="tool_call", args=args)
                    )
                    self.assertEqual(result.status, "error")
                    self.assertIn(f"No changes written: {file_path}:", result.content)
                    self.assertIn(error, result.content)
                    self.assertIn(excerpt, result.content)
                    self.assertEqual(path.read_text(), original)

            result = self.tool_call(
                root,
                "edit_file",
                dict(
                    file_path=file_path,
                    old_string="import unittest",
                    new_string="if True:",
                ),
            )
            self.assertEqual(result.status, "error")
            self.assertIn(f"{file_path}:6: SyntaxError:", result.content)
            self.assertEqual(path.read_text(), original)
            result = self.tool_call(
                root,
                "edit_file",
                dict(
                    file_path=file_path,
                    old_string="import unittest",
                    new_string="import sys",
                ),
            )
            self.assertEqual(result.status, "success", result.content)
            self.assertEqual(path.read_text(), original.replace("unittest", "sys"))

    def test_invalid_python_write_does_not_create_or_track_file(self):
        with tempfile.TemporaryDirectory() as root:
            written = coder.WriteTracker()
            result = self.tool_call(
                root,
                "write_file",
                dict(file_path="new.py", content="return 0\n"),
                written,
            )
            self.assertEqual(result.status, "error")
            self.assertIn(
                "new.py:1: SyntaxError: 'return' outside function", result.content
            )
            self.assertIn("1 | return 0", result.content)
            self.assertFalse(Path(root, "new.py").exists())
            self.assertEqual(written.mutations, 0)

    def test_notifier_rewrites_preserve_imported_names_until_importers_change(self):
        file_path = "backend/notify/prefs_state.py"
        budget = "backend/notify/budget.py"
        router = "backend/notify/router.py"
        exports = '__all__ = ["send_notification"]\n'
        removed = (
            "notifier_lock: object = object()\n"
            "def load_notifier_state():\n"
            "    return {}\n" + exports
        )
        kept = "class State:\n    pass\n\nasync def send_notification():\n    pass\n"
        original = removed + kept
        proposal = kept + "\ndef classify_runner_state():\n    return 'idle'\n"
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, file_path)
            target.parent.mkdir(parents=True)
            target.write_text(original)
            Path(root, budget).write_text(
                "from backend.notify.prefs_state import (\n"
                "    load_notifier_state,\n"
                "    notifier_lock,\n"
                ")\n"
            )
            Path(root, router).write_text(
                "from . import prefs_state as prefs\n"
                "prefs.send_notification()\n"
                "import backend.notify.prefs_state\n"
                "backend.notify.prefs_state.notifier_lock\n"
            )
            Path(root, "untracked.py").write_text(
                "from backend.notify.prefs_state import notifier_lock\n"
            )
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(
                ["git", "-C", root, "add", "--", file_path, budget, router],
                check=True,
            )
            backend = coder.build_filesystem_backend(root)
            edit = coder.build_edit_tool(backend)
            replace = coder.build_replace_lines_tool(backend, {file_path: True})
            written = coder.WriteTracker()
            results = (
                self.tool_call(
                    root,
                    "write_file",
                    {"file_path": file_path, "content": proposal},
                    written,
                ),
                edit.invoke(
                    dict(
                        name=edit.name,
                        id="edit",
                        type="tool_call",
                        args=dict(
                            file_path=file_path, old_string=removed, new_string=""
                        ),
                    )
                ),
                replace.invoke(
                    dict(
                        name=replace.name,
                        id="replace",
                        type="tool_call",
                        args=dict(
                            file_path=file_path, start_line=1, end_line=4, new_text=""
                        ),
                    )
                ),
            )
            for result in results:
                self.assertEqual(result.status, "error")
                self.assertIn(f"No changes written: {file_path}:", result.content)
                self.assertIn(f"load_notifier_state: {budget}:2", result.content)
                self.assertIn(f"notifier_lock: {budget}:3, {router}:4", result.content)
                self.assertIn(f"send_notification: {router}:2", result.content)
                self.assertIn("update the importers in the plan", result.content)
                self.assertNotIn("untracked.py", result.content)
            self.assertEqual(target.read_text(), original)
            self.assertEqual(written.mutations, 0)

            Path(root, budget).write_text("from elsewhere import notifier_lock\n")
            Path(root, router).write_text(
                "from elsewhere import prefs_state\nprefs_state.send_notification()\n"
            )
            self.assertIsNone(backend.write(file_path, proposal).error)
            self.assertEqual(target.read_text(), proposal)

    def test_invalid_typescript_write_uses_transpiler(self):
        from backend.ast_validator import _ts_lib_dir

        if not shutil.which("node") or not _ts_lib_dir():
            self.skipTest("requires TypeScript compiler")
        with tempfile.TemporaryDirectory() as root:
            backend = coder.build_filesystem_backend(root)
            for path, invalid, valid in (
                ("test.ts", "const foo = ;", "const foo: number = 1;"),
                ("test.tsx", "const foo = <div>{}</span>;", "const foo = <div />;"),
            ):
                with self.subTest(path=path):
                    result = backend.write(path, invalid)
                    self.assertIn(f"No changes written: {path}:1:", result.error)
                    self.assertIn(f"1 | {invalid}", result.error)
                    self.assertFalse(Path(root, path).exists())
                    self.assertIsNone(backend.write(path, valid).error)
                    self.assertEqual(Path(root, path).read_text(), valid)

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

    def test_filesystem_search_skips_nested_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory, ".worktrees", "issue-1570")
            archive = checkout / ".worktrees/.queue-retry/previous"
            archive.mkdir(parents=True)
            (archive / "old.py").write_text("last_heartbeat ARCHIVED\n")
            (checkout / "active.py").write_text("last_heartbeat ACTIVE\n")
            (checkout / "alias.py").symlink_to(archive / "old.py")
            backend = coder.build_filesystem_backend(str(checkout))
            self.assertEqual(
                [entry["path"] for entry in backend.ls("/").entries], ["/active.py"]
            )
            self.assertEqual(
                [entry["path"] for entry in backend.glob("**/*.py").matches],
                ["/active.py"],
            )
            self.assertEqual(
                [entry["path"] for entry in backend.grep("last_heartbeat").matches],
                ["/active.py"],
            )
            self.assertIsNotNone(backend.read("/alias.py").error)

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
