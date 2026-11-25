import json
import tempfile
import unittest
from pathlib import Path

from scripts import eval_routing_outcomes


class TestEvalRoutingOutcomes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_job_outcomes_parses_job_and_summary(self):
        job_dir = self.root / "arii_remote-server_1"
        job_dir.mkdir()
        job_file = job_dir / "job.json"
        summary_file = job_dir / "summary.json"

        job_data = {
            "issue": {"repo": "arii/remote-server", "number": 1},
            "routing": {
                "tier": "strong",
                "tier_rule": "task:architectural",
                "features": {"file_count": 2, "is_architectural": True},
                "picks": {
                    "strong": {"model": "openrouter/inclusionai/ling-3.2-flash:free"}
                },
            },
        }
        summary_data = {
            "exit_code": 0,
            "tier": "strong",
            "strong_model": "openrouter/inclusionai/ling-3.2-flash:free",
            "elapsed_s": 15.5,
            "budget": {"parent": {"calls_used": 6}},
        }
        job_file.write_text(json.dumps(job_data))
        summary_file.write_text(json.dumps(summary_data))

        records = eval_routing_outcomes.load_job_outcomes(self.root)
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["issue_key"], "arii/remote-server#1")
        self.assertEqual(rec["tier_suggested"], "strong")
        self.assertEqual(rec["tier_rule"], "task:architectural")
        self.assertEqual(rec["served_tier"], "strong")
        self.assertEqual(
            rec["selected_model"], "openrouter/inclusionai/ling-3.2-flash:free"
        )
        self.assertTrue(rec["success"])
        self.assertEqual(rec["calls_used"], 6)

    def test_load_ledger_decisions(self):
        ledger_file = self.root / "ledger.jsonl"
        rows = [
            {"extra": {"event": "other"}},
            {
                "started": 1000.0,
                "thread_id": "arii/remote-server#2",
                "extra": {
                    "event": "routing-decision",
                    "tier": "strong",
                    "tier_rule": "task:many_files",
                    "features": {"file_count": 4},
                    "picks": {"strong": {"model": "nebius/qwen"}},
                },
            },
        ]
        ledger_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        decisions = eval_routing_outcomes.load_ledger_decisions(ledger_file)
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["issue_key"], "arii/remote-server#2")
        self.assertEqual(decisions[0]["tier_suggested"], "strong")

    def test_aggregate_routing_metrics_and_format_report(self):
        records = [
            {
                "served_tier": "strong",
                "selected_model": "nebius/qwen",
                "tier_rule": "task:architectural",
                "has_finished": True,
                "success": True,
                "calls_used": 5,
                "elapsed_s": 20.0,
            },
            {
                "served_tier": "strong",
                "selected_model": "nebius/qwen",
                "tier_rule": "task:architectural",
                "has_finished": True,
                "success": False,
                "calls_used": 10,
                "elapsed_s": 40.0,
            },
            {
                "served_tier": "default",
                "selected_model": "openrouter/free-code",
                "tier_rule": "task:standard",
                "has_finished": True,
                "success": True,
                "calls_used": 3,
                "elapsed_s": 10.0,
            },
        ]
        aggregates = eval_routing_outcomes.aggregate_routing_metrics(records)
        self.assertEqual(aggregates["total_records"], 3)
        self.assertEqual(aggregates["finished_records"], 3)
        self.assertEqual(len(aggregates["by_model"]), 2)
        self.assertEqual(len(aggregates["by_rule"]), 2)

        report = eval_routing_outcomes.format_report(aggregates)
        self.assertIn("nebius/qwen", report)
        self.assertIn("task:architectural", report)
        self.assertIn("50.0%", report)

    def test_main_cli_queue_dir(self):
        job_dir = self.root / "arii_remote-server_1"
        job_dir.mkdir()
        (job_dir / "job.json").write_text(
            json.dumps({"issue": {"repo": "a", "number": 1}})
        )
        (job_dir / "summary.json").write_text(json.dumps({"exit_code": 0}))

        code = eval_routing_outcomes.main(["--queue-dir", str(self.root), "--json"])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
