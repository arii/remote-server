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


class Choose(unittest.TestCase):
    def setUp(self):
        self.features = {"est_input_tokens": 7000, "file_count": 1, "needs_tools": True}
        self.disabled = {"nvidia", "sambanova"}

    def test_fast_picks_biggest_free_tool_model(self):
        pick = task_router.choose(
            "fast", self.features, CATALOG, disabled=self.disabled
        )
        self.assertEqual(pick["model"], "google/gemini-flash-lite")
        self.assertTrue(pick["free_only"])
        self.assertEqual(pick["rule"], "rank:free_context_desc")
        self.assertNotIn(
            "groq/free-nontools",
            pick["top3"],  # tools filter
        )

    def test_fast_respects_disabled_providers(self):
        self.assertNotIn(
            "nvidia/z-ai/glm-free",
            task_router.choose("fast", self.features, CATALOG, disabled=self.disabled)[
                "top3"
            ],
        )

    def test_fast_context_floor_excludes_tiny_windows(self):
        pick = task_router.choose(
            "fast",
            {"est_input_tokens": 7000, "file_count": 0, "needs_tools": True},
            CATALOG,
            disabled=self.disabled,
        )
        self.assertNotIn("@cf/granite-micro", pick["top3"][0] if pick["top3"] else "")

    def test_strong_picks_cheapest_meeting_floor(self):
        # Free tool-capable models meet the floor at zero cost, so they
        # beat every paid model; the 32k-window model fails the 2x-input floor.
        pick = task_router.choose(
            "strong", self.features, CATALOG, disabled=self.disabled, cap_usd=1.0
        )
        self.assertEqual(pick["model"], "google/gemini-flash-lite")
        self.assertEqual(pick["rule"], "rank:cheapest_meeting_floor")
        self.assertEqual(pick["est_call_cost_usd"], 0.0)
        self.assertFalse(pick["free_only"])
        self.assertIsNone(pick["no_pick_reason"])

    def test_strong_prefers_cheaper_paid_at_same_capability(self):
        paid_only = [m for m in CATALOG if not m["free"]]
        with mock.patch.dict(os.environ, {"TWT_PAID_PROVIDERS": "openrouter,google"}):
            pick = task_router.choose(
                "strong",
                self.features,
                paid_only,
                disabled=self.disabled,
                cap_usd=2.0,
                blocked=set(),
            )
        # nebius Qwen (~$0.01/call) < gemini-pro < frontier/huge (~$0.72/call)
        self.assertEqual(pick["model"], "nebius/Qwen/Qwen3-235B")
        self.assertEqual(
            pick["top3"],
            ["nebius/Qwen/Qwen3-235B", "google/gemini-pro", "openrouter/frontier/huge"],
        )

    def test_strong_blockers_exclude_paid_provider(self):
        paid_only = [m for m in CATALOG if not m["free"]]
        pick = task_router.choose(
            "strong",
            self.features,
            paid_only,
            disabled=self.disabled,
            cap_usd=2.0,
            blocked={"nebius"},
        )
        self.assertIsNone(pick["model"])

    def test_strong_excludes_unrated_and_unfunded_paid(self):
        paid_only = [m for m in CATALOG if not m["free"]]
        pick = task_router.choose(
            "strong", self.features, paid_only, disabled=self.disabled, cap_usd=2.0
        )
        # only nebius can spend by default; unrated/paid fails closed
        self.assertEqual(pick["top3"], ["nebius/Qwen/Qwen3-235B"])

    def test_strong_paid_cap_enforced(self):
        paid_only = [m for m in CATALOG if not m["free"]]
        with mock.patch.dict(os.environ, {"TWT_PAID_PROVIDERS": "openrouter,google"}):
            pick = task_router.choose(
                "strong",
                self.features,
                paid_only,
                disabled=self.disabled,
                cap_usd=0.05,
                blocked=set(),
            )
        # frontier/huge (~$0.72) and gemini-pro (~$0.09) exceed half of $0.05
        self.assertEqual(pick["top3"], ["nebius/Qwen/Qwen3-235B"])
        pick = task_router.choose(
            "strong",
            self.features,
            paid_only,
            disabled=self.disabled,
            cap_usd=0.001,
            blocked=set(),
        )
        self.assertIsNone(pick["model"])
        self.assertIn("capability floor", pick["no_pick_reason"])

    def test_empty_catalog_yields_no_pick(self):
        pick = task_router.choose("fast", self.features, [], disabled=self.disabled)
        self.assertIsNone(pick["model"])
        self.assertEqual(pick["candidates"], 0)


class RouteIssue(unittest.TestCase):
    def test_decision_carries_features_tier_and_picks(self):
        issue = {
            "title": "Fix runs.py summary",
            "body": "touch backend/shared/runs.py and backend/shared/skill_runs.py and docs/x.md",
        }
        decision = task_router.route_issue(
            issue, 1.0, catalog=CATALOG, disabled={"nvidia"}
        )
        self.assertEqual(decision["tier"], "strong")
        self.assertEqual(decision["tier_rule"], "task:many_files")
        self.assertEqual(decision["picks"]["fast"]["model"], "google/gemini-flash-lite")
        self.assertIsNotNone(decision["picks"]["strong"]["model"])

    def test_simple_issue_stays_default(self):
        decision = task_router.route_issue(
            {"title": "typo", "body": "fix a typo"}, 1.0, catalog=CATALOG
        )
        self.assertEqual(decision["tier"], "default")


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
        self.assertIn("strong", captured["extra"]["picks"])


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
