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

    def test_hashes_match_boomtick_vectors(self):
        # Literals copied from boomtick
        # tests/fixtures/agent-ledger/intake-hash-vectors.json so the Python
        # and Apps Script sides cannot drift. Update both together.
        vectors = [
            (
                "Check queue",
                "arii/remote-server",
                "29c9161c27a1e33f33eceb1f87a3f1cb5e431ceb003b7cac51b74b9e30cc91ae",
                "ledger-intake-29c9161c27a1e33f33eceb1f",
            ),
            (
                "CHECK   Queue",
                "Arii/Remote-Server",
                "29c9161c27a1e33f33eceb1f87a3f1cb5e431ceb003b7cac51b74b9e30cc91ae",
                "ledger-intake-29c9161c27a1e33f33eceb1f",
            ),
            (
                "  padded title  ",
                "arii/boomtick",
                "26995a82aca6479eccfc454ccc34b6f9e5d03818f1ba650645ae95ac330dd93d",
                "ledger-intake-26995a82aca6479eccfc454c",
            ),
        ]
        for title, repo, expected_hash, expected_id in vectors:
            with self.subTest(title=title, repo=repo):
                self.assertEqual(dedupe_hash(title, repo), expected_hash)
                self.assertEqual(intake_id(title, repo), expected_id)

    def test_build_row_records_missing_fields_and_needs_info(self):
        row = build_row(
            {"title": "Task", "repo": "arii/remote-server", "problem": "why"}
        )
        self.assertEqual(row["missing_fields"], "scope,done_when")
        self.assertEqual(row["status"], "NEEDS_INFO")
        self.assertEqual(len(row["dedupe_hash"]), 64)
        self.assertEqual(row["canonical_intake_id"], row["intake_id"])
        self.assertEqual(row["agent_issue_url"], "")

    def test_blank_routing_fields_need_info_and_are_never_defaulted(self):
        full = {
            "title": "Task",
            "repo": "arii/remote-server",
            "problem": "why",
            "scope": "backend",
            "done_when": "tests pass",
        }
        for name in ("repo", "scope", "done_when"):
            with self.subTest(name):
                row = build_row({**full, name: "  "})
                self.assertEqual(row["missing_fields"], name)
                self.assertEqual(row["status"], "NEEDS_INFO")
                self.assertEqual(row[name], "  ")
        absent = build_row({"title": "Task"})
        self.assertEqual(absent["missing_fields"], "repo,problem,scope,done_when")
        for name in ("repo", "scope", "done_when"):
            self.assertNotIn(name, absent)

    def test_duplicate_and_ready_states_are_explicit(self):
        complete = {
            "title": "Task",
            "repo": "arii/remote-server",
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
                "repo": "arii/remote-server",
                "problem": "why",
                "scope": "where",
                "done_when": "proof",
            }
        )
        claimed = claim_row(row, "https://github.com/arii/remote-server/issues/1")
        self.assertEqual(claimed["canonical_intake_id"], row["intake_id"])
        self.assertEqual(
            claimed["agent_issue_url"], "https://github.com/arii/remote-server/issues/1"
        )
        self.assertEqual(
            claim_row(claimed, "https://github.com/arii/remote-server/issues/2"),
            claimed,
        )

    def test_claim_requires_issue_url(self):
        with self.assertRaises(ValueError):
            claim_row(build_row({"title": "Task", "repo": "arii/remote-server"}), "")
