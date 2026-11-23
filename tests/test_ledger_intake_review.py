import json
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import ledger_intake_review as review


class LedgerIntakeReviewTests(unittest.TestCase):
    def issue(self, comments=None):
        return {
            "number": 7,
            "title": "Task",
            "body": "Problem: x\nScope: y",
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

        reply = SimpleNamespace(content=json.dumps({
            "missing_fields": ["Done when"],
            "duplicate_url": None,
            "reason": "The acceptance condition is absent.",
        }))
        with (
            mock.patch.object(review.subprocess, "run", side_effect=fake_run),
            mock.patch.object(
                review.model_router,
                "resolve_llm",
                return_value={"provider": "ollama", "model": "qwen3:8b", "base_url": "http://twt/local"},
            ),
            mock.patch.object(
                review.model_router,
                "build_chat_model",
                return_value=(mock.Mock(invoke=mock.Mock(return_value=reply)), {
                    "provider": "ollama", "model": "qwen3:8b", "base_url": "http://twt/local"
                }),
            ),
            mock.patch.object(review.twt_receipts, "run", mock.MagicMock()),
            mock.patch.object(review.ledger, "record") as record,
        ):
            self.assertEqual(review.run("o/r", 7), "comment posted")

        self.assertEqual(len([args for args in commands if args[1:3] == ["issue", "comment"]]), 1)
        self.assertFalse(any("label" in args for args in commands))
        record.assert_called_once()

    def test_existing_review_is_not_called_or_commented(self):
        issue = self.issue([{"body": review.MARKER}])
        with mock.patch.object(review, "issue_payload", return_value=issue), mock.patch.object(
            review, "review"
        ) as model_review:
            self.assertEqual(review.run("o/r", 7), "already reviewed")
        model_review.assert_not_called()

    def test_invalid_duplicate_is_rejected(self):
        with self.assertRaises(RuntimeError):
            review.parse_result(
                json.dumps({
                    "missing_fields": [],
                    "duplicate_url": "https://github.com/o/r/issues/99",
                    "reason": "",
                }),
                {"https://github.com/o/r/issues/8"},
            )

