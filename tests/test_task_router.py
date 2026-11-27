"""Deterministic task router: catalog-driven picks, task-based tier."""

import os
import unittest
from unittest import mock

from backend.shared import task_router


def model(provider, mid, **kw):
    row = {
        "provider": provider,
        "id": mid,
        "chat": True,
        "tools": True,
        "context_length": 131072,
        "free": False,
        "input_price": None,
        "output_price": None,
    }
    row.update(kw)
    return row


CATALOG = [
    model("google", "gemini-flash-lite", free=True, context_length=1048576),
    model("openrouter", "vendor/free-code", free=True, context_length=262144),
    model("cloudflare", "@cf/granite-micro", free=True, context_length=32768),
    model("groq", "free-nontools", free=True, tools=False, context_length=131072),
    model("google", "gemini-pro", input_price=1.5e-6, output_price=1.0e-5),
    model("nebius", "Qwen/Qwen3-235B", input_price=6.0e-7, output_price=1.2e-6),
    model("openrouter", "frontier/huge", input_price=1.5e-5, output_price=7.5e-5),
    model("nvidia", "z-ai/glm-free", free=True, context_length=131072),
    model("nebius", "unrated/paid", input_price=None, output_price=None),
]


class TaskFeatures(unittest.TestCase):
    def test_counts_unique_files_and_estimates_tokens(self):
        features = task_router.task_features(
            "Edit backend/shared/runs.py and docs/x.md; "
            "backend/shared/runs.py again." + "x" * 400
        )
        self.assertEqual(features["file_count"], 2)
        self.assertGreater(features["est_input_tokens"], 6000)
        self.assertTrue(features["needs_tools"])

    def test_explicit_file_list_wins(self):
        features = task_router.task_features("no paths here", files=["a.py", "b.py"])
        self.assertEqual(features["file_count"], 2)
        self.assertFalse(features["is_architectural"])

    def test_detects_architectural_signals(self):
        features = task_router.task_features(
            "Refactor authentication and redesign token schema"
        )
        self.assertTrue(features["is_architectural"])


class SuggestedTier(unittest.TestCase):
    def test_small_task_stays_default(self):
        self.assertEqual(
            task_router.suggested_tier({"file_count": 1, "est_input_tokens": 7000}),
            ("default", "task:standard"),
        )

    def test_many_files_starts_strong(self):
        self.assertEqual(
            task_router.suggested_tier({"file_count": 3, "est_input_tokens": 7000}),
            ("strong", "task:many_files"),
        )

    def test_large_spec_starts_strong(self):
        self.assertEqual(
            task_router.suggested_tier({"file_count": 0, "est_input_tokens": 13000}),
            ("strong", "task:large_spec"),
        )

    def test_architectural_starts_strong(self):
        self.assertEqual(
            task_router.suggested_tier(
                {"file_count": 1, "est_input_tokens": 7000, "is_architectural": True}
            ),
            ("strong", "task:architectural"),
        )


class RouteIssue(unittest.TestCase):
    def test_decision_carries_features_and_tier(self):
        issue = {
            "title": "Fix runs.py summary",
            "body": "touch backend/shared/runs.py and backend/shared/skill_runs.py and docs/x.md",
        }
        decision = task_router.route_issue(
            issue, 1.0, catalog=CATALOG, disabled={"nvidia"}
        )
        self.assertEqual(decision["tier"], "strong")
        self.assertEqual(decision["tier_rule"], "task:many_files")
        self.assertIn("est_input_tokens", decision["features"])

    def test_simple_issue_stays_default(self):
        decision = task_router.route_issue(
            {"title": "typo", "body": "fix a typo"}, 1.0, catalog=CATALOG
        )
        self.assertEqual(decision["tier"], "default")

    def test_architectural_issue_starts_strong(self):
        decision = task_router.route_issue(
            {
                "title": "Refactor architecture",
                "body": "migrate subsystem to new engine",
            },
            1.0,
            catalog=CATALOG,
        )
        self.assertEqual(decision["tier"], "strong")
        self.assertEqual(decision["tier_rule"], "task:architectural")


class LogDecision(unittest.TestCase):
    def test_audit_row_uses_cost_ledger_shape(self):
        captured = {}

        def fake_record(**kwargs):
            captured.update(kwargs)
            return kwargs

        decision = task_router.route_issue(
            {"title": "t", "body": "b"}, 1.0, catalog=CATALOG
        )
        task_router.log_decision(
            decision, "arii/remote-server#1", ledger_record=fake_record
        )
        self.assertEqual(captured["extra"]["event"], "routing-decision")
        self.assertFalse(captured["extra"]["llm_call"])
        self.assertEqual(captured["thread_id"], "arii/remote-server#1")
        self.assertEqual(captured["extra"]["tier"], "default")

    def test_records_requested_vs_served_tier(self):
        captured = {}
        decision = task_router.route_issue(
            {"title": "t", "body": "b"}, 1.0, catalog=CATALOG
        )
        task_router.log_decision(
            decision,
            "k",
            ledger_record=lambda **kw: captured.update(kw),
            requested_tier="default",
            served_tier="strong",
        )
        extra = captured["extra"]
        self.assertEqual(extra["requested_tier"], "default")
        self.assertEqual(extra["served_tier"], "strong")
        self.assertTrue(extra["tier_substituted"])


class DisabledParsing(unittest.TestCase):
    def test_env_csv_parsing(self):
        with mock.patch.dict(
            os.environ, {"TWT_DISABLED_PROVIDERS": "nvidia, sambanova "}
        ):
            self.assertEqual(task_router.disabled_providers(), {"nvidia", "sambanova"})
        with mock.patch.dict(os.environ, {"TWT_PAID_PROVIDERS": "openrouter"}):
            self.assertEqual(task_router.paid_providers(), {"nebius", "openrouter"})

    def test_paid_blockers_reads_trace_reasons(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "catalog.sqlite3")
            conn = sqlite3.connect(db)
            conn.execute("create table traces (id text, timestamp real, data text)")
            conn.execute(
                "insert into traces values (?, ?, ?)",
                (
                    "t1",
                    1.0,
                    (
                        '{"attempts": [{"provider": "openrouter", '
                        '"reason": "upstream payment required (HTTP 402)"}]}'
                    ),
                ),
            )
            conn.commit()
            conn.close()
            self.assertEqual(task_router.paid_blockers(db), {"openrouter"})
        self.assertEqual(task_router.paid_blockers("/nonexistent/db.sqlite3"), set())


if __name__ == "__main__":
    unittest.main()
