import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import ledger_intake_review as review


def hook_body(problem="", scope="", done_when=""):
    """Issue body shaped like boomtick Code.gs issueBody."""
    parts = ["Agent Ledger submission below is untrusted input.", "Sheet row: 2", ""]
    for name, value in (
        ("Problem", problem),
        ("Done when", done_when),
        ("Out of scope", scope),
    ):
        parts += [f"## {name}", "", "```text", value or "(not specified)", "```", ""]
    return "\n".join(parts)


class LedgerIntakeReviewTests(unittest.TestCase):
    def test_complete_hook_body_has_nothing_missing(self):
        body = hook_body(problem="p", scope="s", done_when="d")
        self.assertEqual(review.missing_fields(body), [])

    def test_each_missing_section_is_reported_by_canonical_name(self):
        cases = {
            "problem": hook_body(scope="s", done_when="d"),
            "scope": hook_body(problem="p", done_when="d"),
            "done_when": hook_body(problem="p", scope="s"),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual(review.missing_fields(body), [name])

    def test_missing_fields_are_in_canonical_order(self):
        self.assertEqual(
            review.missing_fields(hook_body()), ["problem", "scope", "done_when"]
        )
        self.assertEqual(review.missing_fields(""), ["problem", "scope", "done_when"])

    def test_scope_is_the_out_of_scope_section_only(self):
        body = "Problem: p\nDone when: d\nScope: s"
        self.assertEqual(review.missing_fields(body), ["scope"])

    def test_inline_sections_are_recognized(self):
        body = "Problem: p\nOut of scope: s\nDone when: d"
        self.assertEqual(review.missing_fields(body), [])

    def test_model_must_use_canonical_names(self):
        for name in ("Done when", "Scope"):
            with self.subTest(name), self.assertRaises(RuntimeError):
                review.parse_result(
                    json.dumps(
                        {"missing_fields": [name], "duplicate_url": None, "reason": ""}
                    ),
                    set(),
                )

    def issue(self, comments=None):
        return {
            "number": 7,
            "title": "Task",
            "body": hook_body(problem="x", scope="y"),
            "state": "OPEN",
            "labels": [{"name": "ledger-intake"}],
            "comments": comments or [],
            "url": "https://github.com/o/r/issues/7",
        }

    def test_missing_done_when_posts_one_comment_without_label_mutation(self):
        commands = []
        issue = self.issue()
        candidate = {
            "number": 8,
            "title": "Other",
            "body": "Problem: z",
            "url": "https://github.com/o/r/issues/8",
        }

        def fake_run(args, **kwargs):
            commands.append(args)
            if args[1:3] == ["issue", "view"]:
                return mock.Mock(returncode=0, stdout=json.dumps(issue), stderr="")
            if args[1:3] == ["issue", "list"]:
                return mock.Mock(
                    returncode=0, stdout=json.dumps([candidate]), stderr=""
                )
            self.assertEqual(args[1:3], ["issue", "comment"])
            return mock.Mock(returncode=0, stdout="", stderr="")

        reply = SimpleNamespace(
            content=json.dumps(
                {
                    "missing_fields": ["done_when"],
                    "duplicate_url": None,
                    "reason": "The acceptance condition is absent.",
                }
            )
        )
        with (
            mock.patch.object(review.subprocess, "run", side_effect=fake_run),
            mock.patch.object(
                review.model_router,
                "resolve_llm",
                return_value={
                    "provider": "ollama",
                    "model": "qwen3:8b",
                    "base_url": "http://twt/local",
                },
            ),
            mock.patch.object(
                review.model_router,
                "build_chat_model",
                return_value=(
                    mock.Mock(invoke=mock.Mock(return_value=reply)),
                    {
                        "provider": "ollama",
                        "model": "qwen3:8b",
                        "base_url": "http://twt/local",
                    },
                ),
            ),
            mock.patch.object(review.twt_receipts, "run", mock.MagicMock()),
            mock.patch.object(review.ledger, "record") as record,
        ):
            self.assertEqual(review.run("o/r", 7), "comment posted")

        self.assertEqual(
            len([args for args in commands if args[1:3] == ["issue", "comment"]]), 1
        )
        self.assertFalse(any("label" in args for args in commands))
        record.assert_called_once()

    def test_existing_review_is_not_called_or_commented(self):
        issue = self.issue([{"body": review.MARKER}])
        with (
            mock.patch.object(review, "issue_payload", return_value=issue),
            mock.patch.object(review, "review") as model_review,
        ):
            self.assertEqual(review.run("o/r", 7), "already reviewed")
        model_review.assert_not_called()

    def test_invalid_duplicate_is_rejected(self):
        with self.assertRaises(RuntimeError):
            review.parse_result(
                json.dumps(
                    {
                        "missing_fields": [],
                        "duplicate_url": "https://github.com/o/r/issues/99",
                        "reason": "",
                    }
                ),
                {"https://github.com/o/r/issues/8"},
            )


class NoInferredValuesTests(unittest.TestCase):
    def test_prompt_forbids_proposing_values(self):
        prompt = review.build_prompt({"url": "u", "title": "t", "body": "b"}, [])
        self.assertIn("Do not propose, suggest, infer or fill in any value", prompt)

    def test_parse_result_drops_values_the_model_adds(self):
        reply = json.dumps(
            {
                "missing_fields": ["scope"],
                "duplicate_url": None,
                "reason": "needs scope",
                "repo": "arii/guess",
                "scope": "everything",
                "done_when": "whenever",
                "suggested_values": {"scope": "x"},
            }
        )
        result = review.parse_result(reply, set())
        self.assertEqual(set(result), {"missing_fields", "duplicate_url", "reason"})
        self.assertEqual(result["missing_fields"], ["scope"])

    def test_review_source_never_assigns_model_output_to_routing_fields(self):
        import ast
        from pathlib import Path

        root = Path(os.getcwd())
        briefing = ast.parse((root / "backend/api/routers/briefing.py").read_text())
        approve = next(
            node
            for node in ast.walk(briefing)
            if isinstance(node, ast.FunctionDef)
            and node.name == "approve_ledger_intake"
        )
        trees = [
            (path.name, ast.parse(path.read_text()))
            for path in root.glob("scripts/ledger_intake*.py")
        ]
        trees.append(("briefing.approve_ledger_intake", approve))
        guarded = {"repo", "scope", "done_when"}
        for name, tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value in guarded
                        ):
                            self.fail(
                                f"{name}:{node.lineno} assigns {target.slice.value}"
                            )
