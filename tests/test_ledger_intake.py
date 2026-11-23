import os
import unittest

from scripts.ledger_intake import (
    build_row,
    claim_row,
    dedupe_hash,
    intake_id,
    ready_rows,
    status_for,
    STATUS_VALUES,
)
from backend.core.intake_risk import risk_categories


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

    def test_duplicate_and_human_ready_states_are_explicit(self):
        complete = {
            "title": "Task",
            "repo": "arii/remote-server",
            "problem": "why",
            "scope": "where",
            "done_when": "proof",
        }
        self.assertEqual(build_row(complete)["status"], "NEW")
        self.assertEqual(build_row(complete, duplicate=True)["status"], "DUPLICATE")
        human_ready = {**build_row(complete), "status": "READY"}
        self.assertEqual(
            ready_rows(
                [build_row(complete), build_row(complete, duplicate=True), human_ready]
            ),
            [human_ready],
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


def _complete(**overrides):
    return {
        "title": "Tidy docs",
        "repo": "arii/remote-server",
        "problem": "wording",
        "scope": "docs",
        "done_when": "merged",
        **overrides,
    }


class NeverAutoReadyTests(unittest.TestCase):
    """Intake never writes READY or DISPATCHED (#1613, mirrors boomtick #1410)."""

    def test_complete_unflagged_row_is_new(self):
        self.assertEqual(status_for(_complete()), "NEW")
        self.assertEqual(build_row(_complete())["status"], "NEW")

    def test_matrix_never_yields_ready_or_dispatched(self):
        rows = [
            _complete(),
            _complete(title="Deploy the backend"),
            _complete(title="Email the customers"),
            _complete(title="Rotate the API key"),
            {},
            {"title": "Task"},
            _complete(repo=""),
            _complete(scope="  "),
            _complete(done_when=""),
            _complete(status="READY"),
            _complete(status="DISPATCHED"),
        ]
        seen = set()
        for row in rows:
            for duplicate in (False, True):
                with self.subTest(row=row, duplicate=duplicate):
                    status = status_for(row, duplicate=duplicate)
                    built = build_row(row, duplicate=duplicate)["status"]
                    self.assertEqual(status, built)
                    self.assertNotIn(status, ("READY", "DISPATCHED"))
                    seen.add(status)
        self.assertEqual(seen, {"NEW", "NEEDS_INFO", "DUPLICATE", "NEEDS_APPROVAL"})

    def test_ready_and_dispatched_remain_valid_statuses(self):
        self.assertIn("READY", STATUS_VALUES)
        self.assertIn("DISPATCHED", STATUS_VALUES)


class HighBlastRadiusLockTests(unittest.TestCase):
    def test_categories_detected_on_sample_rows(self):
        samples = {
            "production_mutation": [
                "Deploy the backend",
                "run docker compose up on the host",
                "DROP TABLE users",
                "rm -rf the cache dir",
                "Restart services after the change",
            ],
            "external_comms": [
                "Email the customers",
                "Publish the release notes",
                "post to Slack",
                "send an ntfy alert",
            ],
            "billing_permission": [
                "Raise the spend cap",
                "change IAM roles",
                "rotate token scopes",
                "enable branch protection",
                "grant admin",
            ],
        }
        for category, titles in samples.items():
            for title in titles:
                with self.subTest(title=title):
                    self.assertIn(category, risk_categories({"title": title}))

    def test_common_phrasings_are_detected(self):
        samples = {
            "redeploy to prod": "production_mutation",
            "Roll out to production": "production_mutation",
            "we will roll it out on Friday": "production_mutation",
            "force push to main": "production_mutation",
            "git push --force": "production_mutation",
            "delete the prod database": "production_mutation",
            "Rotate the API key": "billing_permission",
            "ntfy the team": "external_comms",
            "update IAM": "billing_permission",
        }
        for title, category in samples.items():
            with self.subTest(title=title):
                self.assertIn(category, risk_categories({"title": title}))

    def test_short_tokens_match_whole_words_only(self):
        for title in ("iambic parser", "ntfyx helper", "Pliam notes"):
            with self.subTest(title=title):
                self.assertEqual(risk_categories({"title": title}), [])

    def test_other_fields_are_scanned(self):
        self.assertEqual(
            risk_categories({"done_when": "customers receive an email"}),
            ["external_comms"],
        )

    def test_plain_row_has_no_categories(self):
        self.assertEqual(risk_categories(_complete()), [])

    def test_complete_flagged_row_locks_to_needs_approval(self):
        row = build_row(_complete(title="Deploy the backend"))
        self.assertEqual(row["status"], "NEEDS_APPROVAL")
        self.assertEqual(row["risk_categories"], "production_mutation")

    def test_unflagged_complete_row_is_unchanged(self):
        self.assertEqual(build_row(_complete())["status"], "NEW")

    def test_precedence_of_duplicate_and_missing_fields_is_unchanged(self):
        flagged = _complete(title="Deploy the backend")
        self.assertEqual(status_for(flagged, duplicate=True), "DUPLICATE")
        self.assertEqual(status_for({**flagged, "scope": ""}), "NEEDS_INFO")

    def test_needs_approval_never_reaches_ready_rows(self):
        row = build_row(_complete(title="Deploy the backend"))
        self.assertEqual(ready_rows([row]), [])

    def test_queue_runner_has_no_intake_status_path(self):
        # The queue dispatches only on the GitHub `ready` label. Nothing in
        # it may read intake status or promote NEEDS_APPROVAL to that label.
        path = os.path.join(os.getcwd(), "scripts", "queue_runner.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for needle in ("NEEDS_APPROVAL", "ledger_intake", "intake_risk"):
            self.assertNotIn(needle, source)
        self.assertIn('if "ready" not in labels', source)
