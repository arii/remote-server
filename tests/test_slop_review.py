import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import slop_review

PASS_REPLY = json.dumps({"verdict": "READY", "findings": [], "summary": "clean"})
FAIL_REPLY = json.dumps(
    {
        "verdict": "NEEDS-WORK",
        "findings": [{"file": "a.py", "line": 3, "note": "placeholder success"}],
        "summary": "slop found",
    }
)


class FakePipeline:
    """Mirrors Pipeline.run: submit failure is non-APPROVED, else hand-off."""

    log = None

    def stage_submit(self, worktree_root, title, body=""):
        self.log.append("submit")
        return True, "https://github.com/o/r/pull/9"

    def run(self):
        ok, url = self.stage_submit("/wt", "t", "b")
        if not ok:
            return {"status": "CHANGES_REQUESTED", "detail": url}
        self.log.append("handoff")
        return {"status": "APPROVED", "submit_url": url}


class SlopReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        skill = self.root / slop_review.SKILL_PATH
        skill.parent.mkdir(parents=True)
        skill.write_text("# skill\nBe hostile.\n")
        self.log = []
        self.model_calls = 0
        self.ledger_rows = 0
        self.reply = PASS_REPLY
        self.post_ok = True

    def model_factory(self):
        def model_fn(prompt):
            self.model_calls += 1
            self.log.append("model")
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        def ledger_fn():
            self.ledger_rows += 1

        return model_fn, ledger_fn

    def run_pipeline(self, diff="diff --git a/a.py b/a.py\n+x\n"):
        state = {}
        cls = type("P", (FakePipeline,), {"log": self.log})
        cls.stage_submit = slop_review.gated_submit(
            cls.stage_submit,
            state,
            root=self.root,
            gh=["gh"],
            model_factory=self.model_factory,
            diff_fn=lambda wt: self.log.append("diff") or diff,
        )

        def post(gh, url, body):
            self.log.append("comment")
            self.bodies.append(body)
            return (True, "") if self.post_ok else (False, "gh down")

        self.bodies = []
        with mock.patch.object(slop_review, "post_comment", post):
            value = cls().run()
        return slop_review.override_result(value, state), state

    def test_pass_posts_comment_before_handoff(self):
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "APPROVED")
        self.assertEqual(self.log, ["submit", "diff", "model", "comment", "handoff"])
        self.assertIn("PASS", self.bodies[0])

    def test_fail_comments_and_blocks_handoff(self):
        self.reply = FAIL_REPLY
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_FAILED")
        self.assertNotIn("handoff", self.log)
        self.assertEqual(self.log.count("comment"), 1)
        self.assertIn("a.py:3", self.bodies[0])

    def test_malformed_output_fails_closed(self):
        self.reply = "looks fine to me"
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_FAILED")
        self.assertIn("malformed", value["detail"])
        self.assertNotIn("handoff", self.log)
        self.assertEqual(self.log.count("comment"), 1)

    def test_model_error_fails_closed_and_still_counts_ledger_row(self):
        self.reply = RuntimeError("boom")
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_FAILED")
        self.assertEqual(self.ledger_rows, self.model_calls)

    def test_missing_skill_is_config_failure_with_comment(self):
        (self.root / slop_review.SKILL_PATH).unlink()
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_CONFIG")
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.log.count("comment"), 1)
        self.assertNotIn("handoff", self.log)

    def test_empty_skill_is_config_failure(self):
        (self.root / slop_review.SKILL_PATH).write_text("  \n")
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_CONFIG")

    def test_oversized_diff_fails_closed_without_model_call(self):
        value, _ = self.run_pipeline("x" * (slop_review.MAX_DIFF_CHARS + 1))
        self.assertEqual(value["status"], "SLOP_REVIEW_FAILED")
        self.assertIn("diff too large", value["detail"])
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.log.count("comment"), 1)

    def test_comment_post_failure_fails_closed(self):
        self.post_ok = False
        value, _ = self.run_pipeline()
        self.assertEqual(value["status"], "SLOP_REVIEW_FAILED")
        self.assertIn("comment not posted", value["detail"])
        self.assertNotIn("handoff", self.log)

    def test_one_ledger_row_per_model_call(self):
        self.run_pipeline()
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(self.ledger_rows, 1)

    def test_comment_is_capped(self):
        result = {
            "status": "FAIL",
            "verdict": "BLOCK",
            "truncated": False,
            "detail": "d",
            "findings": [{"file": "f.py", "line": 1, "note": "n" * 200}] * 100,
        }
        self.assertLessEqual(
            len(slop_review.format_comment(result)), slop_review.MAX_COMMENT_CHARS
        )

    def test_failed_submit_is_not_reviewed(self):
        cls = type("P", (FakePipeline,), {"log": self.log})
        original = lambda self, *a, **k: (False, "td failed")  # noqa: E731
        wrapped = slop_review.gated_submit(
            original,
            {},
            root=self.root,
            gh=["gh"],
            model_factory=self.model_factory,
        )
        self.assertEqual(wrapped(cls(), "/wt"), (False, "td failed"))
        self.assertEqual(self.model_calls, 0)

    def test_post_comment_pipes_body_to_gh(self):
        done = mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch.object(slop_review.subprocess, "run", return_value=done) as run:
            self.assertEqual(slop_review.post_comment(["gh"], "u", "body"), (True, ""))
        self.assertEqual(
            run.call_args.args[0], ["gh", "pr", "comment", "u", "--body-file", "-"]
        )
        self.assertEqual(run.call_args.kwargs["input"], "body")

    def test_checkout_root_reads_s5_checkout(self):
        with mock.patch.dict(os.environ, {"S5_CHECKOUT": "/x"}):
            self.assertEqual(slop_review.checkout_root(), Path("/x"))


if __name__ == "__main__":
    unittest.main()
