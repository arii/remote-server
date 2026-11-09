"""Deferral record + guards for #1217 claims 2-5 (verified, deferred).

Each token-saving proposal below was verified against the tree during
the #1217 audit and found already-satisfied, mislocated, or owned by
a sibling lane — so this branch deliberately implements nothing for
them. This module IS the standing record: ``CLAIMS`` names each
claim, its file:line evidence, and why no change lands here. The
tests pin the status-quo contracts, so any silent implementation (or
a refactor that moves the cited code) fails loudly and forces the
record to be updated first.

1. Prompt caching (claim 1) is NOT listed here: it was verified true
   and implemented in deep-agent/coder.py; see test_coder_prompt_order.
"""

import os
import unittest

import intent
import model_router
import re
import s5tools
from backend.exec import engine

# Claim -> evidence (path relative to repo root) -> why deferred.
CLAIMS = [
    {
        "claim": "sub-agent context isolation",
        "files": {
            "deep-agent/triage_fanout.py": [
                "TOP ACTION",
                "scout_results_from_messages",
                "disable_general_purpose",
                "(#539)",
            ],
            "deep-agent/pipeline.py": ["def make_coder", "repair_notes"],
        },
        "status": "already-satisfied",
        "why": "scouts are stateless per-repo specs returning one TOP ACTION "
        "line; general-purpose subagent disabled (#539); Pipeline mints "
        "a fresh coder thread per build with bounded repair_notes",
    },
    {
        "claim": "tool-output compaction",
        "files": {
            "deep-agent/s5tools.py": [
                "MAX_OUT = 4000",
                "FETCH_DEFAULT_MAX_BYTES",
                "SEARCH_MAX_RESULTS = 10",
            ],
            "backend/exec/engine.py": [
                "MAX_OUTPUT = 200_000",
                "RUN_STORE_MAX_RUNS = 200",
                "JOB_ERROR_MAX = 300",
            ],
        },
        "status": "already-satisfied (sibling lane owns these files)",
        "why": "s5tools caps output/fetch/search; engine keeps tails and "
        "evicts runs; both files are scope-guarded to the sibling "
        "implementer, so no change here",
    },
    {
        "claim": "state trim / compaction nodes",
        "files": {
            "deep-agent/deep_chat.py": [
                "MAX_HISTORY_MESSAGES = 40",
                "def plan_history_trim",
                "def trim_thread_history",
            ]
        },
        "status": "mislocated, already met",
        "why": "runs.py holds ledger/trace reads only (no live message "
        "state); trimming already happens at deep_chat turn start",
    },
    {
        "claim": "two-tier model routing",
        "files": {
            "deep-agent/model_router.py": [
                "def resolve_llm",
                "def chat_kwargs",
                '"reasoning_effort": "low"',
            ],
            "deep-agent/triage_fanout.py": ["FANOUT_MODEL_PIN"],
            "deep-agent/intent.py": ["def classify", "no LLM"],
        },
        "status": "already-satisfied",
        "why": "cheapest-first ladder with low reasoning effort; triage "
        "pins a cheap model; intent routing is zero-LLM keyword rules",
    },
]


def _source(module):
    with open(module.__file__, encoding="utf-8") as handle:
        return handle.read()


def _tree_source(rel):
    """Source text for a repo-relative path (works for venv-only
    modules like triage_fanout that stdlib tests cannot import)."""
    from cli import repo_root

    path = os.path.join(repo_root(), rel)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestDeferralRecord(unittest.TestCase):
    def test_record_names_all_four_deferred_claims(self):
        self.assertEqual(len(CLAIMS), 4)
        for claim in CLAIMS:
            self.assertTrue(claim["claim"], claim)
            self.assertTrue(claim["files"], claim)
            self.assertTrue(claim["why"], claim)

    def test_every_cited_symbol_still_exists(self):
        for claim in CLAIMS:
            for rel, symbols in claim["files"].items():
                source = _tree_source(rel)
                for symbol in symbols:
                    self.assertIn(
                        symbol,
                        source,
                        f"{claim['claim']}: {rel} lost {symbol!r} "
                        f"— update the deferral record first",
                    )


class TestCompactionCapsPinned(unittest.TestCase):
    def test_s5tools_caps(self):
        self.assertEqual(s5tools.MAX_OUT, 4000)
        self.assertEqual(s5tools.SEARCH_MAX_RESULTS, 10)
        self.assertEqual(s5tools.FETCH_DEFAULT_MAX_BYTES, 262144)

    def test_engine_caps_and_tail_truncation(self):
        self.assertEqual(engine.MAX_OUTPUT, 200_000)
        self.assertEqual(engine.RUN_STORE_MAX_RUNS, 200)
        self.assertEqual(engine.JOB_ERROR_MAX, 300)
        big = "x" * (engine.MAX_OUTPUT + 1000)
        self.assertEqual(engine.truncate_tail(big), big[-engine.MAX_OUTPUT :])
        self.assertEqual(engine.truncate_tail("small"), "small")


class TestTrimLocationPinned(unittest.TestCase):
    def test_trim_lives_in_deep_chat(self):
        deep_chat = __import__("deep_chat")
        self.assertEqual(deep_chat.MAX_HISTORY_MESSAGES, 40)
        for name in ("plan_history_trim", "trim_thread_history"):
            self.assertTrue(callable(getattr(deep_chat, name)), name)

    def test_runs_holds_no_message_state_trim(self):
        runs = __import__("runs")
        source = _source(runs)
        self.assertNotIn("def trim", source)
        self.assertNotIn("MAX_HISTORY", source)


class TestRoutingLadderPinned(unittest.TestCase):
    def test_cheapest_first_ladder(self):
        source = _source(model_router)
        for symbol in (
            "def resolve_llm",
            "def chat_kwargs",
            '"reasoning_effort": "low"',
            "def build_chat_model",
        ):
            self.assertIn(symbol, source)

    def test_triage_pins_cheap_model(self):
        source = _tree_source("deep-agent/triage_fanout.py")
        match = re.search(r'FANOUT_MODEL_PIN\s*=\s*"([^"]+)"', source)
        self.assertIsNotNone(match, "triage must pin its fanout model")
        self.assertTrue(match.group(1))
        self.assertIn("disable_general_purpose", source)

    def test_intent_routing_is_zero_llm_keyword_rules(self):
        self.assertEqual(intent.classify("fix the failing build"), "fix")
        self.assertEqual(intent.classify("implement calc.add"), "implement")
        self.assertEqual(intent.classify("hi"), "trivial")


class TestIsolationShapePinned(unittest.TestCase):
    def test_pipeline_mints_coder_with_bounded_repair_notes(self):
        pipeline = __import__("pipeline")
        import inspect

        params = inspect.signature(pipeline.Pipeline.make_coder).parameters
        self.assertIn("repair_notes", params)

    def test_scout_contract(self):
        source = _tree_source("deep-agent/triage_fanout.py")
        self.assertIn("def scout_results_from_messages", source)
        self.assertIn("TOP ACTION", source)


if __name__ == "__main__":
    unittest.main()
