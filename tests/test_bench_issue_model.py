import unittest
from types import SimpleNamespace
import asyncio

from scripts import bench_issue_model as bench

SPEC = """# Spec

## Verification Commands
```bash
python3 -m pytest backend/tests/test_skill_runs.py
# a comment
python3 -m unittest backend.tests.test_agent_runs_repo
```

*Blocking Questions:*
"""


class BenchHelpersTests(unittest.TestCase):
    def test_verification_commands_come_from_the_fenced_block(self):
        self.assertEqual(
            bench.verification_commands(SPEC),
            [
                "python3 -m pytest backend/tests/test_skill_runs.py",
                "python3 -m unittest backend.tests.test_agent_runs_repo",
            ],
        )
        self.assertEqual(bench.verification_commands("# no section\n"), [])

    def test_run_coder_tuple_is_read_without_dict_access(self):
        # The #1571 bench crashed here: run_coder returns a tuple, not a dict.
        result = ("Wrote it.\nDONE #1571", {"input_tokens": 0}, [], 1.0, "twt:x")
        self.assertEqual(bench.terminal(bench.reply_text(result)), "DONE")
        self.assertEqual(bench.terminal("stuck\nBLOCKED #1571"), "BLOCKED")
        self.assertIsNone(bench.terminal(bench.reply_text(("no marker", {}))))
        self.assertEqual(bench.reply_text({"unexpected": "dict"}), "")

    def test_valid_needs_done_changes_and_passing_checks_not_just_a_return(self):
        good = {
            "run_returned": True,
            "terminal": "DONE",
            "changed_files": ["backend/shared/runs.py"],
            "verification": [{"command": "x", "exit": 0}],
            "gateway": {"proof": True},
        }
        self.assertTrue(bench.judge(good))
        for change in (
            {"gateway": {"proof": False}},
            {"gateway": {}},
            {"run_returned": False},
            {"terminal": "BLOCKED"},
            {"terminal": None},
            {"changed_files": []},
            {"verification": []},
            {"verification": [{"command": "x", "exit": 1}]},
            {"verification": [{"command": "x", "exit": "timeout"}]},
            {"strict_violations": ["non-local model served: nebius/x"]},
        ):
            with self.subTest(change=change):
                self.assertFalse(bench.judge({**good, **change}))

    def test_gateway_proof_needs_trace_ids(self):
        none = bench.gateway_proof(set(), set(), [])
        self.assertFalse(none["proof"])
        self.assertIn(
            "no TwT trace IDs: run did not go through the gateway",
            bench.strict_violations("native", none),
        )
        local = {
            "trace_id": "a",
            "provider": "ollama-local",
            "model": "qwen3:8b",
            "cost_usd": 0.0,
            "cost_complete": True,
        }
        proof = bench.gateway_proof(
            {"a", "b"}, {"a", "b"}, [local, {**local, "trace_id": "b"}]
        )
        self.assertTrue(proof["proof"])
        self.assertEqual(proof["served"], ["ollama-local/qwen3:8b"])
        self.assertEqual(bench.strict_violations("native", proof), [])
        missing = bench.gateway_proof({"a", "b"}, {"a", "b"}, [local])
        self.assertFalse(missing["proof"])
        self.assertIn(
            "a trace has no TwT receipt", bench.strict_violations("native", missing)
        )

    def test_strict_local_flags_hosted_service_and_cost(self):
        hosted = bench.gateway_proof(
            {"a"},
            set(),
            [
                {
                    "trace_id": "a",
                    "provider": "nebius",
                    "model": "m",
                    "cost_usd": 0.01,
                    "cost_complete": True,
                }
            ],
        )
        self.assertEqual(
            bench.strict_violations("native", hosted),
            ["non-local model served: nebius/m", "nonzero cost: 0.01"],
        )

    def test_receipts_fail_closed_on_identity_cost_or_pin(self):
        row = {
            "trace_id": "a",
            "provider": "nebius",
            "model": "m",
            "cost_complete": True,
            "cost_usd": 0.1,
        }
        self.assertTrue(bench.gateway_proof({"a"}, set(), [row], "nebius/m")["proof"])
        for rows, pin in (
            ([row, row], "nebius/m"),
            ([{**row, "trace_id": "wrong"}], "nebius/m"),
            ([row], "nebius/other"),
            ([{**row, "cost_usd": None}], "nebius/m"),
            ([{**row, "cost_complete": False}], "nebius/m"),
            ([{**row, "cost_usd": float("nan")}], "nebius/m"),
            ([{**row, "cost_usd": -1}], "nebius/m"),
        ):
            with self.subTest(rows=rows, pin=pin):
                self.assertFalse(bench.gateway_proof({"a"}, set(), rows, pin)["proof"])
        self.assertEqual(bench.STRICT_LOCAL_CAP_USD, 0)

    def test_strict_local_refuses_hosted_before_capture(self):
        seen = []
        router = SimpleNamespace(_remote_spec=lambda *a: "hosted")
        receipts = SimpleNamespace(capture=lambda req: seen.append(req.url.path))
        bench.install_strict_local_guard(router, receipts)
        with self.assertRaises(RuntimeError):
            router._remote_spec("nebius", "x", "https://example.invalid")
        for path in ("/v1/chat/completions", "/api/chat"):
            request = SimpleNamespace(url=SimpleNamespace(path=path))
            with self.assertRaises(RuntimeError):
                receipts.capture(request)
            with self.assertRaises(RuntimeError):
                asyncio.run(receipts.acapture(request))
        self.assertEqual(seen, [])
        local = SimpleNamespace(url=SimpleNamespace(path="/local/api/chat"))
        receipts.capture(local)
        asyncio.run(receipts.acapture(local))
        self.assertEqual(seen, ["/local/api/chat", "/local/api/chat"])

    def test_routes_and_strict_local_reject_hosted_pins(self):
        with self.assertRaises(SystemExit):
            bench.parse_args(["nebius/x", "--strict-local"])
        with self.assertRaises(SystemExit):
            bench.parse_args(["ollama-local/qwen3:8b", "--strict-local"])
        with self.assertRaises(SystemExit):
            bench.parse_args(["ollama-local/qwen3:8b"])
        with self.assertRaises(SystemExit):
            bench.parse_args(["nebius/x", "--route", "native"])
        args = bench.parse_args(["ollama-local/qwen3:8b", "--route", "native"])
        env = bench.environment(args, bench.pathlib.Path("/tmp/x"))
        self.assertEqual(env["S5_LOCAL_ONLY"], "1")
        self.assertEqual(env["ASK_MODEL"], "qwen3:8b")
        strict = bench.environment(
            bench.parse_args(
                ["ollama-local/qwen3:8b", "--route", "native", "--strict-local"]
            ),
            bench.pathlib.Path("/tmp/x"),
        )
        self.assertEqual(strict["CODER_FALLBACK_MODEL"], "")
        hosted = bench.environment(
            bench.parse_args(["nebius/x"]), bench.pathlib.Path("/tmp/x")
        )
        self.assertEqual(hosted["CODER_MODEL"], "nebius/x")


if __name__ == "__main__":
    unittest.main()
