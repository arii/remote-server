"""Coder budget: planner-owned splitting, bounded resume, parent caps.

Reason for a new file: these tests cover one feature (split + resume +
caps) across pipeline.py and coder.py and do not fit one existing file.
Models are faked, no network; roots come from tempfile.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import coder
import deep_chat
import observe
import pipeline
from backend.shared import ledger

try:
    import deepagents  # noqa: F401
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

    class FakeMessagesListChatModel:
        pass


def brief(items, context_lines=0, files=()):
    lines = ["# Add retry handling", "Intro paragraph."]
    lines += [f"context line {n}" for n in range(context_lines)]
    lines += ["", "## Acceptance"] + [f"- [ ] {item}" for item in items]
    if files:
        lines += ["", "### Allowed Files"] + [f"- `{path}`" for path in files]
    return "\n".join(lines) + "\n"


SMALL = brief(["it works"])
BIG = brief([f"item {n}" for n in range(1, 8)], context_lines=70)


class ScriptedPipeline(pipeline.Pipeline):
    """Pipeline with a fixed spec and scripted coder outcomes."""

    def __init__(self, spec, outcomes, root, pre_audit=None, **kwargs):
        super().__init__(
            repo="/tmp",
            intent="intent",
            pre_audit_fn=pre_audit or (lambda _root: (True, None, "")),
            **kwargs,
        )
        self.spec = spec
        self.outcomes = list(outcomes)
        self.root = root
        self.briefs = []
        self.audits = 0
        self.submits = 0

    def stage_init(self):
        return True, {"status": "ready"}

    def stage_spec(self):
        return True, {}, self.spec

    def stage_code(self, brief, worktree_root=None, repair_notes=""):
        self.briefs.append(brief)
        outcome = self.outcomes.pop(0)
        self.stage_calls = outcome.get("calls", 0)
        self.stage_continuations = outcome.get("continuations", 0)
        self.stage_exhausted = outcome.get("exhausted", False)
        if not outcome.get("ok", True):
            return False, {}, outcome.get("reason", "coder failed")
        return True, {}, self.root

    def stage_audit(self, worktree_root):
        self.audits += 1
        return pipeline.APPROVED, {}, "ok"

    def stage_submit(self, worktree_root, title, body=""):
        self.submits += 1
        return True, "https://example.invalid/pr/1"

    def stage_cleanup(self, worktree_root):
        return True, ""


class PipelineBudgetCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.state = os.path.join(self.root, "budget.json")
        patch = mock.patch.dict(os.environ, {"S5_BUDGET_STATE": self.state})
        patch.start()
        self.addCleanup(patch.stop)

    def run_pipeline(self, spec, outcomes, **kwargs):
        pipe = ScriptedPipeline(spec, outcomes, self.root, submit_pr=True, **kwargs)
        return pipe, pipe.run()

    def saved(self):
        return json.loads(Path(self.state).read_text())


class TestSplitting(PipelineBudgetCase):
    def test_small_brief_is_not_split_and_gets_a_one_entry_record(self):
        pipe, result = self.run_pipeline(SMALL, [{"calls": 7}])
        self.assertEqual(result["status"], pipeline.APPROVED)
        self.assertEqual(pipe.briefs, [SMALL])
        state = self.saved()
        self.assertEqual(len(state["sub_briefs"]), 1)
        self.assertEqual(state["sub_briefs"][0]["status"], "done")
        self.assertEqual(state["sub_briefs"][0]["calls"], 7)
        self.assertEqual(state["parent"]["calls_used"], 7)
        self.assertTrue(state["parent"]["verified"])
        self.assertEqual(result["budget"], state)

    def test_large_brief_splits_into_bounded_parts_with_own_acceptance(self):
        parts = pipeline.split_brief(BIG)
        self.assertEqual(len(parts), pipeline.SPLIT_MAX_PARTS)
        self.assertEqual(
            [item for part in parts for item in part["acceptance"]],
            [f"item {n}" for n in range(1, 8)],
        )
        for part in parts:
            lines, _ = pipeline.brief_size(part["brief"])
            self.assertLessEqual(lines, pipeline.BRIEF_LINE_BUDGET)
            self.assertIn("Add retry handling", part["brief"])
            for item in part["acceptance"]:
                self.assertIn(f"- {item}", part["brief"])
        self.assertNotIn("item 7", parts[0]["brief"])

    def test_split_runs_sequentially_then_one_audit_and_submit(self):
        outcomes = [{"calls": 10}, {"calls": 12}, {"calls": 3}]
        pipe, result = self.run_pipeline(BIG, outcomes)
        self.assertEqual(result["status"], pipeline.APPROVED)
        self.assertEqual(len(pipe.briefs), 3)
        self.assertEqual((pipe.audits, pipe.submits), (1, 1))
        state = self.saved()
        self.assertEqual([s["calls"] for s in state["sub_briefs"]], [10, 12, 3])
        self.assertEqual(state["parent"]["calls_used"], 25)
        self.assertTrue(all(s["verified"] for s in state["sub_briefs"]))
        spec_file = Path(self.root, pipe.spec_path)
        self.assertEqual(spec_file.read_text(), BIG)

    def test_file_count_over_budget_also_splits(self):
        files = [f"src/mod{n}.py" for n in range(pipeline.BRIEF_FILE_BUDGET + 1)]
        spec = brief(["part a", "part b"], files=files)
        self.assertEqual(pipeline.brief_size(spec)[1], len(files))
        pipe, result = self.run_pipeline(spec, [{}, {}])
        self.assertEqual(len(pipe.briefs), 2)
        self.assertEqual(result["status"], pipeline.APPROVED)
        for sub in pipe.briefs:
            for path in files:
                self.assertIn(f"`{path}`", sub)

    def test_unsplittable_over_budget_brief_never_runs_the_coder(self):
        spec = brief(["only one item"], context_lines=70)
        pipe, result = self.run_pipeline(spec, [])
        self.assertEqual(result["status"], pipeline.BUDGET_UNSPLITTABLE)
        self.assertEqual(pipe.briefs, [])
        self.assertIn("Acceptance", result["detail"])
        self.assertEqual(pipe.submits, 0)
        self.assertEqual(self.saved()["sub_briefs"][0]["status"], "failed")


class TestPartialCompletion(PipelineBudgetCase):
    def test_failed_sub_brief_blocks_the_next_and_reports_partial(self):
        pipe, result = self.run_pipeline(
            BIG, [{"calls": 5}, {"ok": False, "calls": 4, "reason": "boom"}]
        )
        self.assertEqual(result["status"], pipeline.BLOCKED)
        self.assertEqual(len(pipe.briefs), 2)
        self.assertEqual(pipe.submits, 0)
        self.assertEqual(result["partial"]["done"], ["item 1", "item 2", "item 3"])
        self.assertEqual(
            result["partial"]["not_done"], [f"item {n}" for n in range(4, 8)]
        )
        self.assertEqual(self.saved()["sub_briefs"][1]["status"], "failed")

    def test_pre_audit_failure_blocks_the_next_sub_brief(self):
        pipe, result = self.run_pipeline(
            BIG, [{}, {}, {}], pre_audit=lambda _r: (False, "mechanical", "bad")
        )
        self.assertEqual(result["status"], pipeline.BLOCKED)
        self.assertEqual(len(pipe.briefs), 1)
        self.assertEqual(result["partial"]["done"], [])
        self.assertEqual(pipe.submits, 0)

    def test_exhausted_sub_brief_is_budget_exhausted_without_a_pr(self):
        outcomes = [
            {"calls": 20},
            {"ok": False, "calls": 21, "continuations": 1, "exhausted": True},
        ]
        pipe, result = self.run_pipeline(BIG, outcomes)
        self.assertEqual(result["status"], pipeline.BUDGET_EXHAUSTED)
        self.assertEqual(pipe.submits, 0)
        self.assertEqual(pipe.audits, 0)
        self.assertEqual(len(pipe.briefs), 2)
        self.assertEqual(result["partial"]["done"], ["item 1", "item 2", "item 3"])
        self.assertEqual(len(result["partial"]["not_done"]), 4)
        state = self.saved()
        self.assertTrue(state["parent"]["exhausted"])
        self.assertEqual(state["parent"]["calls_used"], 41)
        self.assertEqual(state["sub_briefs"][1]["status"], "exhausted")
        self.assertEqual(state["sub_briefs"][1]["continuations"], 1)
        self.assertNotEqual(result["status"], pipeline.BLOCKED)
        self.assertIn("--retry", result["detail"])

    def test_small_brief_exhaustion_is_budget_exhausted(self):
        _, result = self.run_pipeline(
            SMALL, [{"ok": False, "calls": 41, "exhausted": True}]
        )
        self.assertEqual(result["status"], pipeline.BUDGET_EXHAUSTED)
        self.assertEqual(result["partial"]["not_done"], ["it works"])


class TestParentCap(PipelineBudgetCase):
    def test_parent_cap_stops_before_the_next_sub_brief(self):
        outcomes = [{"calls": 30}, {"calls": 30}, {"calls": 1}]
        pipe, result = self.run_pipeline(BIG, outcomes)
        self.assertEqual(result["status"], pipeline.BUDGET_EXHAUSTED)
        self.assertEqual(len(pipe.briefs), 2)
        self.assertEqual(len(pipe.outcomes), 1)
        state = self.saved()
        self.assertEqual(state["parent"]["calls_used"], pipeline.MAX_CALLS_PER_PARENT)
        self.assertEqual(state["sub_briefs"][2]["calls"], 0)
        self.assertEqual(pipe.submits, 0)

    def test_calls_left_shrinks_with_each_stage(self):
        pipe = ScriptedPipeline(SMALL, [], self.root)
        pipe.budget = pipeline.new_budget(
            [{"id": 1, "title": "t", "acceptance": ["a"]}]
        )
        pipe.budget["parent"]["calls_used"] = 25
        pipe.stage_calls = 10
        self.assertEqual(pipe.calls_left(), pipeline.MAX_CALLS_PER_PARENT - 35)

    @unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
    def test_implement_passes_remaining_budget_and_continuation_allowance(self):
        pipe = ScriptedPipeline(SMALL, [], self.root)
        pipe.budget = pipeline.new_budget(
            [{"id": 1, "title": "t", "acceptance": ["a"]}]
        )
        pipe.budget["parent"]["calls_used"] = 25
        pipe._begin_stage(pipe.budget["sub_briefs"][0])
        graph = pipe.make_coder(SMALL)
        seen = {}

        def fake_run_coder(*args, **kwargs):
            seen.update(kwargs)
            kwargs["meter"].update(calls=9, continuations=1)
            return ("DONE #1", {}, [], 0.0, "x:y", "trace", True, 0)

        with (
            mock.patch.object(coder, "run_coder", side_effect=fake_run_coder),
            mock.patch.object(coder, "build_system", return_value="system"),
            mock.patch.object(ledger, "record"),
        ):
            graph.implement_fn("task", "plan", self.root, [])
        self.assertEqual(seen["call_budget"], pipeline.MAX_CALLS_PER_PARENT - 25)
        self.assertEqual(seen["continuations"], pipeline.MAX_CONTINUATIONS_PER_SUBBRIEF)
        self.assertEqual((pipe.stage_calls, pipe.stage_continuations), (9, 1))
        self.assertEqual(pipe.calls_left(), pipeline.MAX_CALLS_PER_PARENT - 25 - 9)

    def test_limits_are_unchanged(self):
        self.assertEqual(coder.INITIAL_MODEL_LIMIT, 20)
        self.assertEqual(coder.REPAIR_MODEL_LIMIT, 12)
        self.assertEqual(
            pipeline.MAX_CALLS_PER_PARENT,
            pipeline.SPLIT_MAX_PARTS * coder.INITIAL_MODEL_LIMIT,
        )


class TestRunCoderResume(unittest.TestCase):
    def cap_error(self, count):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        error = ModelCallLimitExceededError(count, count, count, None)
        error.coder_model_spec = {"provider": "x", "model": "y"}
        error.coder_written_files = ("a.py",)
        return error

    def call(self, **kwargs):
        meter = {}
        return meter, coder.run_coder(
            "/tmp", "brief", "system", "thread", 10, meter=meter, **kwargs
        )

    @unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
    def test_cap_hit_resumes_same_thread_from_the_checkpointed_count(self):
        with mock.patch.object(
            coder, "_run_coder_attempt", side_effect=[self.cap_error(20), "done"]
        ) as attempt:
            meter, result = self.call(continuations=1)
        self.assertEqual(result, "done")
        first, second = attempt.call_args_list
        self.assertEqual(first.args[3], second.args[3])
        self.assertEqual(second.args[1], coder.CONTINUE_PROMPT)
        self.assertEqual(second.kwargs["thread_floor"], 20)
        self.assertEqual(second.kwargs["prior_files"], ("a.py",))
        self.assertEqual(meter["continuations"], 1)

    @unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
    def test_continuations_are_capped_with_exact_attempt_count(self):
        errors = [self.cap_error(20), self.cap_error(40), self.cap_error(60)]
        with mock.patch.object(
            coder, "_run_coder_attempt", side_effect=errors
        ) as attempt:
            with self.assertRaises(type(errors[0])) as raised:
                self.call(continuations=1)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(raised.exception.thread_count, 40)

    @unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
    def test_no_continuation_by_default(self):
        with mock.patch.object(
            coder, "_run_coder_attempt", side_effect=[self.cap_error(20)]
        ) as attempt:
            with self.assertRaises(type(self.cap_error(1))):
                self.call()
        self.assertEqual(attempt.call_count, 1)


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestRealGraphResume(unittest.TestCase):
    def model(self, writes, finish):
        class Model(FakeMessagesListChatModel):
            def bind_tools(self, *args, **kwargs):
                return self

        calls = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"file_path": f"f-{n}.txt", "content": "x"},
                        "id": f"w{n}",
                        "type": "tool_call",
                    }
                ],
            )
            for n in range(writes)
        ]
        if finish:
            calls.append(AIMessage(content="DONE #1"))
        return Model(responses=calls)

    def run_real(self, model, **kwargs):
        from langgraph.checkpoint.memory import InMemorySaver

        saver = InMemorySaver()
        meter = {}
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(
                coder,
                "build_model",
                return_value=(model, {"provider": "local", "model": "fake"}),
            ),
            mock.patch.object(
                deep_chat, "open_checkpointer", return_value=(saver, False, None)
            ),
            mock.patch.object(
                observe,
                "default_trace_path",
                side_effect=lambda _, thread: str(Path(root, thread + ".jsonl")),
            ),
            mock.patch.object(coder, "verify_rounds", return_value=0),
            mock.patch.object(coder, "build_agent", wraps=coder.build_agent) as build,
            mock.patch.object(ledger, "record"),
        ):
            error = None
            result = None
            try:
                result = coder.run_coder(
                    root, "implement", "system", "thread", 30, meter=meter, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - asserted by callers
                error = exc
        floors = [c.kwargs["thread_floor"] for c in build.call_args_list]
        threads = set(saver.storage)
        return result, error, meter, floors, threads

    def test_resume_gets_a_fresh_budget_on_the_same_thread(self):
        result, error, meter, floors, threads = self.run_real(
            self.model(writes=24, finish=True), continuations=1, call_budget=60
        )
        self.assertIsNone(error)
        self.assertEqual(result[0], "DONE #1")
        self.assertEqual(meter.pop("trace").rsplit("/", 1)[-1], "thread.jsonl")
        self.assertEqual(meter, {"calls": 25, "continuations": 1})
        self.assertEqual(floors, [0, 20])
        self.assertEqual(threads, {"thread"})

    def test_second_cap_hit_stops_after_exactly_two_turns(self):
        _, error, meter, floors, _ = self.run_real(
            self.model(writes=60, finish=False), continuations=1, call_budget=60
        )
        self.assertEqual(type(error).__name__, "ModelCallLimitExceededError")
        self.assertEqual(meter.pop("trace").rsplit("/", 1)[-1], "thread.jsonl")
        self.assertEqual(meter, {"calls": 40, "continuations": 1})
        self.assertEqual(floors, [0, 20])

    def test_parent_call_budget_is_a_hard_stop(self):
        _, error, meter, _, _ = self.run_real(
            self.model(writes=60, finish=False), continuations=5, call_budget=30
        )
        self.assertEqual(type(error).__name__, "ModelCallLimitExceededError")
        self.assertEqual(meter["calls"], 30)
        self.assertEqual(meter["continuations"], 1)


if __name__ == "__main__":
    unittest.main()
