"""Stubbed tests for the opt-in CLI fallback (issue #1610).

No live subscription calls, no live model calls, no interactive
auth: every boundary (CLI runner, Ledger, gh, TwT) is a fake.
"""

import json
import sys
import tempfile
import unittest
from unittest import mock

import cli_fallback as cf


class StatusErr(Exception):
    def __init__(self, msg, status):
        super().__init__(msg)
        self.status_code = status


class ModelCallLimitExceededError(Exception):
    pass


class FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records argv; answers binary probes, auth probes, CLI runs."""

    def __init__(
        self, *, present=(), authed=(), cli_stdout="done", cli_code=0, cli_stderr=""
    ):
        self.present = set(present)
        self.authed = set(authed)
        self.cli_stdout = cli_stdout
        self.cli_code = cli_code
        self.cli_stderr = cli_stderr
        self.calls = []

    def __call__(self, argv, *, timeout, cwd=None):
        self.calls.append(list(argv))
        if argv[:3] == ["sh", "-c", 'command -v "$1"']:
            name = argv[-1]
            return FakeResult(0 if name in self.present else 1)
        if argv == ["true"]:
            return FakeResult(0)
        if argv == ["false"]:
            return FakeResult(1)
        # Anything else is the CLI spawn (turn_argv output).
        return FakeResult(self.cli_code, self.cli_stdout, self.cli_stderr)


class FakeGh:
    def __init__(self, prs=(), fail=False):
        self.prs = list(prs)
        self.fail = fail
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            return FakeResult(1, "", "gh boom")
        return FakeResult(0, json.dumps(self.prs))


class FakeTwt:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.posts = []

    def __call__(self, entry):
        if self.fail:
            raise RuntimeError("TwT down")
        self.posts.append(dict(entry))
        return {"receipt_id": "rcpt-1"}


def _config(tmp, **kw):
    kw.setdefault("state_dir", tmp)
    kw.setdefault("clock", lambda: 1000.0)
    kw.setdefault("enabled", True)
    return cf.CliFallbackConfig(**kw)


def _ledger(rows):
    """Fake ledger reader returning rows (latest last)."""
    return lambda link: [dict(r) for r in rows]


class ClassifyTests(unittest.TestCase):
    def test_gateway_statuses(self):
        for status in (500, 502, 503, 504):
            self.assertEqual(
                cf.classify_twt_failure([StatusErr("x", status)]),
                "gateway_unavailable",
                status,
            )

    def test_gateway_connection_errors(self):
        for msg in ("connection refused", "timed out", "bad gateway"):
            self.assertEqual(
                cf.classify_twt_failure([RuntimeError(msg)]), "gateway_unavailable", msg
            )

    def test_budget_blocked(self):
        self.assertEqual(
            cf.classify_twt_failure([StatusErr("pay up", 402)]), "budget_blocked"
        )
        self.assertEqual(
            cf.classify_twt_failure([RuntimeError("quota exceeded")]), "budget_blocked"
        )

    def test_routes_exhausted_only_when_all_retryable(self):
        self.assertEqual(
            cf.classify_twt_failure(
                [RuntimeError("rate limit"), RuntimeError("overloaded")]
            ),
            "routes_exhausted",
        )
        self.assertIsNone(
            cf.classify_twt_failure(
                [RuntimeError("rate limit"), RuntimeError("bad request")]
            )
        )

    def test_exclusions(self):
        self.assertIsNone(cf.classify_twt_failure([ModelCallLimitExceededError("cap")]))
        self.assertIsNone(cf.classify_twt_failure([RuntimeError("generic boom")]))
        self.assertIsNone(
            cf.classify_twt_failure([RuntimeError("bad gateway")], stopped=True)
        )
        self.assertIsNone(cf.classify_twt_failure([]))


class ConfigTests(unittest.TestCase):
    def test_off_by_default(self):
        cfg = cf.CliFallbackConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.order, cf.FALLBACK_ORDER)

    def test_order_validation(self):
        self.assertEqual(cf.validate_order(["muse", "agy"]), ("muse", "agy"))
        for bad in (["nope"], ["agy", "agy"], []):
            with self.assertRaises(ValueError, msg=repr(bad)):
                cf.validate_order(bad)


class SelectTests(unittest.TestCase):
    def test_no_auth_probe_means_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("agy",))
            cfg = _config(tmp, runner=runner)
            provider, probes = cf.select_cli(cfg.order, config=cfg, deadline=2000.0)
            self.assertIsNone(provider)
            self.assertEqual(
                probes[0]["reason"], "no verified non-interactive auth probe"
            )

    def test_ordered_missing_authed_unauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("copilot", "muse"), authed=("muse",))
            cfg = _config(tmp, runner=runner)
            with mock.patch.dict(
                cf.AUTH_PROBE_COMMANDS, {"copilot": ["false"], "muse": ["true"]}
            ):
                provider, probes = cf.select_cli(cfg.order, config=cfg, deadline=2000.0)
            # agy missing, copilot authed=false, kiro-cli missing,
            # muse authed -> muse wins (last in order, first ready).
            self.assertEqual(provider, "muse")
            self.assertEqual(probes[0]["reason"], "binary missing")
            self.assertFalse(probes[1]["authenticated"])

    def test_all_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, runner=FakeRunner())
            provider, probes = cf.select_cli(cfg.order, config=cfg, deadline=2000.0)
            self.assertIsNone(provider)
            self.assertTrue(all(p["reason"] == "binary missing" for p in probes))

    def test_expired_deadline_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp, runner=FakeRunner(present=("agy",)))
            provider, probes = cf.select_cli(cfg.order, config=cfg, deadline=999.0)
            self.assertIsNone(provider)
            self.assertEqual(probes[0]["reason"], "deadline expired")

    def test_authed_but_no_headless_argv_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("copilot",))
            cfg = _config(tmp, runner=runner)
            with mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"copilot": ["true"]}):
                provider, _ = cf.select_cli(cfg.order, config=cfg, deadline=2000.0)
            self.assertIsNone(provider)


class ClaimTests(unittest.TestCase):
    LINK = "https://github.com/arii/remote-server/issues/1"

    def _claim(self, tmp, rows, prs=(), gh_fail=False, post_fail=False):
        posts = []

        def poster(link, run_id):
            if post_fail:
                raise RuntimeError("post down")
            posts.append((link, run_id))

        cfg = _config(
            tmp,
            ledger_reader=_ledger(rows),
            ledger_post=poster,
            gh_runner=FakeGh(prs, fail=gh_fail),
        )
        return cfg, posts

    def test_no_rows_posts_and_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Reader returns [] first, then our Act row on re-read.
            calls = {"n": 0}

            def reader(link):
                calls["n"] += 1
                if calls["n"] == 1:
                    return []
                return [{"Link": self.LINK, "Status": "Act", "Agent / Source": "run-1"}]

            cfg = _config(
                tmp,
                ledger_reader=reader,
                ledger_post=lambda link, r: None,
                gh_runner=FakeGh(),
            )
            self.assertEqual(
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1"),
                "run-1",
            )

    def test_unreadable_ledger_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(
                tmp,
                ledger_reader=lambda link: (_ for _ in ()).throw(
                    RuntimeError("sheet down")
                ),
                gh_runner=FakeGh(),
            )
            with self.assertRaises(cf.Blocked):
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1")

    def test_fresh_act_claim_by_other_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._claim(
                tmp, [{"Link": self.LINK, "Status": "Act", "Agent / Source": "Kiro"}]
            )
            with self.assertRaises(cf.Blocked):
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1")

    def test_open_kiro_pr_for_issue_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs = [
                {
                    "number": 9,
                    "title": "fix #1",
                    "body": "",
                    "author": {"login": "kiro-agent[bot]"},
                    "headRefName": "kiro/fix-1",
                }
            ]
            cfg, _ = self._claim(
                tmp,
                [{"Link": self.LINK, "Status": "Later", "Agent / Source": "S5-Queue"}],
                prs=prs,
            )
            with self.assertRaises(cf.Blocked):
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1")

    def test_unrelated_kiro_pr_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            prs = [
                {
                    "number": 9,
                    "title": "fix #99",
                    "body": "",
                    "author": {"login": "kiro-agent[bot]"},
                    "headRefName": "other/branch",
                }
            ]
            cfg, _ = self._claim(
                tmp,
                [{"Link": self.LINK, "Status": "Later", "Agent / Source": "S5-Queue"}],
                prs=prs,
            )
            self.assertEqual(
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1"),
                "run-1",
            )

    def test_gh_failure_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._claim(
                tmp,
                [{"Link": self.LINK, "Status": "Later", "Agent / Source": "S5-Queue"}],
                gh_fail=True,
            )
            with self.assertRaises(cf.Blocked):
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1")

    def test_claim_post_failure_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, _ = self._claim(tmp, [], post_fail=True)
            with self.assertRaises(cf.Blocked):
                cf.check_claim("arii/remote-server", 1, config=cfg, run_id="run-1")


class JournalTests(unittest.TestCase):
    def test_no_double_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _config(tmp)
            cf.begin_attempt(
                config=cfg,
                attempt_id="a1",
                issue="i#1",
                run_id="r1",
                stage="code",
                cli="agy",
                trigger="gateway_unavailable",
            )
            with self.assertRaises(cf.Blocked):
                cf.begin_attempt(
                    config=cfg,
                    attempt_id="a1",
                    issue="i#1",
                    run_id="r1",
                    stage="code",
                    cli="agy",
                    trigger="gateway_unavailable",
                )


class RunTests(unittest.TestCase):
    LINK = "https://github.com/arii/remote-server/issues/1"

    def _run(self, tmp, **over):
        runner = over.pop("runner", FakeRunner(present=("agy",)))
        twt = over.pop("twt", FakeTwt())
        posts = []
        reads = {"n": 0}

        def reader(link):
            reads["n"] += 1
            if reads["n"] == 1:
                return []
            return [{"Link": link, "Status": "Act", "Agent / Source": "run-1"}]

        def poster(link, run_id):
            posts.append((link, run_id))

        cfg = _config(
            tmp,
            runner=runner,
            twt_post=twt,
            ledger_reader=reader,
            ledger_post=poster,
            gh_runner=FakeGh(),
            **over,
        )
        ledger_posts = []

        def result_post(*, issue_link, note, state, run_id):
            ledger_posts.append((issue_link, note, state))

        with mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}):
            out = cf.run_cli_fallback(
                config=cfg,
                issue="arii/remote-server#1",
                run_id="run-1",
                stage="code",
                attempt_id="att-1",
                trigger="gateway_unavailable",
                brief="fix it",
                worktree=tmp,
                deadline=2000.0,
                repo="arii/remote-server",
                number=1,
                verify_fn=lambda w: (True, None, ""),
                ledger_post_fn=result_post,
            )
        return out, twt, posts, ledger_posts, runner

    def test_full_success_records_twt_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, twt, _posts, ledger_posts, _runner = self._run(tmp)
            _summary, usage, calls, _started, label, _trace, verified, _rounds = out
            self.assertEqual(label, "cli:agy")
            self.assertTrue(verified)
            self.assertEqual(calls, 0)
            self.assertEqual(usage, {})
            # The TwT audit entry is the authoritative record: acked,
            # unknown cost, unknown model/tokens, trigger + provenance.
            self.assertEqual(len(twt.posts), 1)
            entry = twt.posts[0]
            self.assertEqual(entry["cli"], "agy")
            self.assertEqual(entry["trigger"], "gateway_unavailable")
            self.assertEqual(entry["cost_basis"], "unknown")
            self.assertIsNone(entry["cost_usd"])
            self.assertEqual(entry["model"], "unknown")
            self.assertEqual(entry["tokens"], "unknown")
            self.assertEqual(entry["attempt_id"], "att-1")
            self.assertTrue(ledger_posts)
            self.assertIn("rcpt-1", ledger_posts[0][1])

    def test_disabled_is_blocked_and_starts_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("agy",))
            cfg = _config(tmp, enabled=False, runner=runner)
            with self.assertRaises(cf.Blocked):
                cf.run_cli_fallback(
                    config=cfg,
                    issue="i#1",
                    run_id="r1",
                    stage="code",
                    attempt_id="a1",
                    trigger="gateway_unavailable",
                    brief="b",
                    worktree=tmp,
                    deadline=2000.0,
                    repo="arii/remote-server",
                    number=1,
                )
            self.assertEqual(runner.calls, [])

    def test_cli_nonzero_exit_audits_then_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("agy",), cli_code=3, cli_stderr="boom")
            twt = FakeTwt()
            with self.assertRaises(RuntimeError):
                self._run(tmp, runner=runner, twt=twt)
            self.assertEqual(len(twt.posts), 1)
            self.assertEqual(twt.posts[0]["outcome"], "failed")

    def test_verification_failure_audits_then_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("agy",))
            twt = FakeTwt()
            reads = {"n": 0}

            def reader(link):
                reads["n"] += 1
                if reads["n"] == 1:
                    return []
                return [{"Link": link, "Status": "Act", "Agent / Source": "r1"}]

            with (
                self.assertRaises(RuntimeError),
                mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}),
            ):
                cfg = _config(
                    tmp,
                    runner=runner,
                    twt_post=twt,
                    ledger_reader=reader,
                    ledger_post=lambda link, r: None,
                    gh_runner=FakeGh(),
                )
                cf.run_cli_fallback(
                    config=cfg,
                    issue="i#1",
                    run_id="r1",
                    stage="code",
                    attempt_id="a2",
                    trigger="gateway_unavailable",
                    brief="b",
                    worktree=tmp,
                    deadline=2000.0,
                    repo="arii/remote-server",
                    number=1,
                    verify_fn=lambda w: (False, "test_failure", "nope"),
                )
            self.assertEqual(len(twt.posts), 1)
            self.assertEqual(twt.posts[0]["outcome"], "verification_failed")

    def test_twt_down_journals_pending_then_reconciles_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = FakeRunner(present=("agy",))
            down = FakeTwt(fail=True)
            with self.assertRaises(RuntimeError):
                self._run(tmp, runner=runner, twt=down)
            self.assertEqual(down.posts, [])
            # The entry is journaled pending; reconcile once it acks.
            entry = cf.external_audit_entry(
                audit_id="att-1",
                issue="arii/remote-server#1",
                run_id="run-1",
                stage="code",
                attempt_id="att-1",
                cli="agy",
                trigger="gateway_unavailable",
                started_at=1.0,
                outcome="succeeded",
                worktree=tmp,
            )
            up = FakeTwt()
            cfg = _config(tmp, twt_post=up)
            ok, receipt = cf.reconcile_external_audit("att-1", entry, config=cfg)
            self.assertTrue(ok)
            self.assertEqual(receipt, "rcpt-1")
            self.assertEqual(len(up.posts), 1)
            # Second reconcile does not re-post (idempotent).
            ok, receipt = cf.reconcile_external_audit("att-1", entry, config=cfg)
            self.assertTrue(ok)
            self.assertEqual(len(up.posts), 1)

    def test_known_zero_cost_only_when_justified(self):
        with tempfile.TemporaryDirectory() as tmp:
            _out, twt, _, _, _ = self._run(tmp)
            self.assertEqual(twt.posts[0]["cost_basis"], "unknown")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                cf.KNOWN_ZERO_COST, {"agy": "flat sub, no metered use"}
            ):
                _out, twt2, _, _, _ = self._run(tmp)
            self.assertEqual(twt2.posts[0]["cost_basis"], "known_zero")
            self.assertEqual(twt2.posts[0]["cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()


class RunCoderWiringTests(unittest.TestCase):
    """run_coder -> CLI fallback wiring: TwT error in, CLI result out.

    langchain is not installed in this venv, so the cap-error module
    is stubbed in sys.modules and _run_coder_attempt is patched.
    """

    def _stub_langchain_cap(self):
        import types

        for name in ("langchain", "langchain.agents", "langchain.agents.middleware"):
            sys.modules.setdefault(name, types.ModuleType(name))
        cap = types.ModuleType("langchain.agents.middleware.model_call_limit")
        cap.ModelCallLimitExceededError = ModelCallLimitExceededError
        sys.modules["langchain.agents.middleware.model_call_limit"] = cap

    def _cli_config(self, tmp):
        reads = {"n": 0}

        def reader(link):
            reads["n"] += 1
            if reads["n"] == 1:
                return []
            return [{"Link": link, "Status": "Act", "Agent / Source": "run-1"}]

        runner = FakeRunner(present=("agy",))
        cfg = _config(
            tmp,
            runner=runner,
            twt_post=FakeTwt(),
            ledger_reader=reader,
            ledger_post=lambda link, run_id: None,
            gh_runner=FakeGh(),
        )
        return cfg, runner

    def test_gateway_error_triggers_cli_when_enabled(self):
        import coder

        self._stub_langchain_cap()
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner = self._cli_config(tmp)
            with (
                mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}),
                mock.patch.object(
                    coder, "_run_coder_attempt", side_effect=RuntimeError("bad gateway")
                ),
            ):
                out = coder.run_coder(
                    tmp,
                    "fix it",
                    "sys",
                    "thread-1",
                    60,
                    cli_fallback=cfg,
                    issue="arii/remote-server#1",
                    run_id="run-1",
                    cli_verify_fn=lambda w: (True, None, ""),
                )
            self.assertEqual(out[4], "cli:agy")
            self.assertTrue(out[6])
            self.assertTrue(any("agy" in " ".join(c) for c in runner.calls))

    def test_disabled_config_reraises_twt_error(self):
        import coder

        self._stub_langchain_cap()
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner = self._cli_config(tmp)
            cfg.enabled = False
            with (
                mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}),
                mock.patch.object(
                    coder, "_run_coder_attempt", side_effect=RuntimeError("bad gateway")
                ),
            ):
                with self.assertRaises(RuntimeError):
                    coder.run_coder(
                        tmp,
                        "fix it",
                        "sys",
                        "thread-1",
                        60,
                        cli_fallback=cfg,
                        issue="arii/remote-server#1",
                        run_id="run-1",
                        cli_verify_fn=lambda w: (True, None, ""),
                    )
            self.assertEqual(runner.calls, [])

    def test_call_cap_never_triggers_cli(self):
        import coder

        self._stub_langchain_cap()
        err = ModelCallLimitExceededError("cap")
        err.thread_count = 0
        err.thread_limit = 10
        err.coder_written_files = ()
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner = self._cli_config(tmp)
            with (
                mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}),
                mock.patch.object(coder, "_run_coder_attempt", side_effect=err),
            ):
                with self.assertRaises(ModelCallLimitExceededError):
                    coder.run_coder(
                        tmp,
                        "fix it",
                        "sys",
                        "thread-1",
                        60,
                        cli_fallback=cfg,
                        issue="arii/remote-server#1",
                        run_id="run-1",
                        cli_verify_fn=lambda w: (True, None, ""),
                    )
            self.assertEqual(runner.calls, [])

    def test_generic_error_reraises_without_cli(self):
        import coder

        self._stub_langchain_cap()
        with tempfile.TemporaryDirectory() as tmp:
            cfg, runner = self._cli_config(tmp)
            with (
                mock.patch.dict(cf.AUTH_PROBE_COMMANDS, {"agy": ["true"]}),
                mock.patch.object(
                    coder,
                    "_run_coder_attempt",
                    side_effect=RuntimeError("generic boom"),
                ),
            ):
                with self.assertRaises(RuntimeError):
                    coder.run_coder(
                        tmp,
                        "fix it",
                        "sys",
                        "thread-1",
                        60,
                        cli_fallback=cfg,
                        issue="arii/remote-server#1",
                        run_id="run-1",
                        cli_verify_fn=lambda w: (True, None, ""),
                    )
            self.assertEqual(runner.calls, [])
