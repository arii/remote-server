import unittest

from scripts.ledger_intake import (
    build_row,
    claim_row,
    dedupe_hash,
    intake_id,
    ready_rows,
)


class LedgerIntakeTests(unittest.TestCase):
    def test_key_and_hash_are_stable_across_case_and_whitespace(self):
        self.assertEqual(
            dedupe_hash("  Fix   Queue ", " ARII/REMOTE-SERVER "),
            dedupe_hash("fix queue", "arii/remote-server"),
        )
        self.assertEqual(
            intake_id("  Fix   Queue ", " ARII/REMOTE-SERVER "),
            "ledger-intake-" + dedupe_hash("fix queue", "arii/remote-server")[:24],
        )

    def test_build_row_records_missing_fields_and_needs_info(self):
        row = build_row({"title": "Task", "repo": "o/r", "problem": "why"})
        self.assertEqual(row["missing_fields"], "scope,done_when")
        self.assertEqual(row["status"], "NEEDS_INFO")
        self.assertEqual(len(row["dedupe_hash"]), 64)
        self.assertEqual(row["canonical_intake_id"], row["intake_id"])
        self.assertEqual(row["agent_issue_url"], "")

    def test_duplicate_and_ready_states_are_explicit(self):
        complete = {
            "title": "Task",
            "repo": "o/r",
            "problem": "why",
            "scope": "where",
            "done_when": "proof",
        }
        self.assertEqual(build_row(complete)["status"], "READY")
        self.assertEqual(build_row(complete, duplicate=True)["status"], "DUPLICATE")
        self.assertEqual(
            [
                row["status"]
                for row in ready_rows(
                    [build_row(complete), build_row(complete, duplicate=True)]
                )
            ],
            ["READY"],
        )

    def test_dispatched_is_not_ready(self):
        self.assertEqual(ready_rows([{"status": "DISPATCHED"}]), [])

    def test_claim_sets_source_back_reference_once(self):
        row = build_row(
            {
                "title": "Task",
                "repo": "o/r",
                "problem": "why",
                "scope": "where",
                "done_when": "proof",
            }
        )
        claimed = claim_row(row, "https://github.com/o/r/issues/1")
        self.assertEqual(claimed["canonical_intake_id"], row["intake_id"])
        self.assertEqual(claimed["agent_issue_url"], "https://github.com/o/r/issues/1")
        self.assertEqual(
            claim_row(claimed, "https://github.com/o/r/issues/2"),
            claimed,
        )

    def test_claim_requires_issue_url(self):
        with self.assertRaises(ValueError):
            claim_row(build_row({"title": "Task", "repo": "o/r"}), "")
