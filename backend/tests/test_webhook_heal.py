"""Tests for the webhook-pull healing ladder (#855).

Every skip reason proves heal-or-escalate: dirty trees heal via the
stash-and-pull rung (or skip with the stash ref + cause ledgered),
every other reason escalates with its precise cause and a Retry pull
tap. The ladder is never destructive: no reset --hard, checkout,
rebase, push, or stash drop/clear anywhere in its argv.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from backend import autofile
from backend import webhook_heal as H
from backend import notify as N

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import attention
    from backend.api.routers import notify as notify_router

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False

GIT = ["git", "-c", "safe.directory=/tmp", "-C", "/tmp",
       "-c", "core.hooksPath=/dev/null"]

OBSERVE = [
    (0, "aaa\n", ""),   # rev-parse HEAD
    (0, "", ""),        # fetch
    (0, "refs/remotes/origin/main\n", ""),  # symbolic-ref HEAD
    (0, "ooo\n", ""),   # rev-parse --verify origin/main
    (0, "ooo\n", ""),   # rev-parse origin/main
    (0, "3\n", ""),     # rev-list lag
    (0, "0\n", ""),     # rev-list ahead
]

OWN_STASH = "stash@{0}: webhook-heal 1700000000\n"

# The failure words History/autofile treat as failures: heal notes
# must never carry one, so skips stay nudges (autofile stays out).
FAIL_WORDS = re.compile(
    r"fail|error|refus|reject|conflict|not permitted|aborted", re.I)


def _scripted(script, sep=None):
    """subprocess.run stub replaying (code, stdout, stderr)."""
    calls = []
    it = iter(script)

    def fake_run(argv, **kw):
        calls.append(argv)
        code, out, err = next(it)
        return SimpleNamespace(returncode=code, stdout=out,
                               stderr=err)

    return fake_run, calls


def _pull(script):
    fake_run, calls = _scripted(script)
    orig = subprocess.run
    subprocess.run = fake_run
    H.reset_counts()
    try:
        return N.webhook_pull("/tmp"), calls
    finally:
        subprocess.run = orig


def _heal(script, target="origin/main", old_head="aaa"):
    fake_run, calls = _scripted(script)
    H.reset_counts()
    return H.heal_dirty_tree(GIT, {}, target, old_head,
                             run=fake_run), calls


class HealDirtyTreeTest(unittest.TestCase):
    def test_round_trip_heals_and_advances(self):
        res, calls = _heal([
            (0, "", ""),          # stash list: none held
            (0, "Saved\n", ""),   # stash push
            (0, OWN_STASH, ""),   # stash list: ours
            (0, "Updating aaa..ooo\n", ""),  # merge --ff-only
            (0, "ooo\n", ""),     # rev-parse HEAD
            (0, "Dropped\n", ""),  # stash pop
        ])
        self.assertTrue(res["attempted"])
        self.assertTrue(res["healed"])
        self.assertTrue(res["advanced"])
        self.assertEqual(res["outcome"], H.HEAL_ROUND_TRIP)
        self.assertIsNone(res["stash_ref"])
        self.assertEqual(res["merge_line"], "Updating aaa..ooo")
        self.assertEqual(
            calls,
            [GIT + ["stash", "list", "--format=%gd:%gs"],
             GIT + ["stash", "push", "-m", mock.ANY],
             GIT + ["stash", "list", "--format=%gd:%gs"],
             GIT + ["merge", "--ff-only", "origin/main"],
             GIT + ["rev-parse", "HEAD"],
             GIT + ["stash", "pop", "stash@{0}"]])
        self.assertIn(H.STASH_MARKER, calls[1][-1])

    def test_pop_conflict_holds_stash_and_skips(self):
        res, calls = _heal([
            (0, "", ""),
            (0, "Saved\n", ""),
            (0, OWN_STASH, ""),
            (0, "Updating aaa..ooo\n", ""),
            (0, "ooo\n", ""),
            (1, "", "CONFLICT: needs a hand"),  # stash pop refused
            (0, OWN_STASH, ""),  # re-list confirms the hold
        ])
        self.assertTrue(res["attempted"])
        self.assertFalse(res["healed"])
        # The checkout moved but the tree needs a hand: advanced
        # stays False so the skip remains operator-visible.
        self.assertFalse(res["advanced"])
        self.assertEqual(res["outcome"], H.HEAL_STASH_HELD)
        self.assertEqual(res["stash_ref"], "stash@{0}")
        self.assertIn("stash@{0}", res["note"])
        # The entry is kept, never dropped.
        verbs = [c[len(GIT)] for c in calls]
        self.assertNotIn("drop", verbs)
        self.assertEqual(calls[-2][len(GIT):],
                         ["stash", "pop", "stash@{0}"])

    def test_stash_refused_escalates(self):
        res, calls = _heal([
            (0, "", ""),
            (1, "", "error: cannot stash"),  # stash push refused
        ])
        self.assertFalse(res["healed"])
        self.assertFalse(res["advanced"])
        self.assertEqual(res["outcome"], H.HEAL_STASH_FAILED)
        self.assertIsNone(res["stash_ref"])
        # Nothing past the refused stash is attempted.
        self.assertEqual(len(calls), 2)

    def test_prior_holding_escalates_without_new_stash(self):
        res, calls = _heal([
            (0, "stash@{0}: webhook-heal 1699999999\n", ""),
        ])
        self.assertFalse(res["healed"])
        self.assertEqual(res["outcome"], H.HEAL_PRIOR_HOLDING)
        self.assertEqual(res["stash_ref"], "stash@{0}")
        # One read-only list: no second stash is ever stacked.
        self.assertEqual(
            calls, [GIT + ["stash", "list", "--format=%gd:%gs"]])

    def test_untracked_only_pulls_through(self):
        res, calls = _heal([
            (0, "", ""),
            (0, "No local changes to save\n", ""),
            (0, "", ""),  # no entry created: nothing to restore
            (0, "Updating aaa..ooo\n", ""),
            (0, "ooo\n", ""),
        ])
        self.assertTrue(res["healed"])
        self.assertTrue(res["advanced"])
        self.assertEqual(res["outcome"], H.HEAL_PULL_THROUGH)
        self.assertIsNone(res["stash_ref"])
        self.assertNotIn(GIT + ["stash", "pop", mock.ANY], calls)

    def test_blocked_merge_restores_tree(self):
        res, calls = _heal([
            (0, "", ""),
            (0, "Saved\n", ""),
            (0, OWN_STASH, ""),
            (1, "", "error: merge blocked"),  # merge refused
            (0, "Dropped\n", ""),  # restore pop succeeds
        ])
        self.assertFalse(res["healed"])
        self.assertEqual(res["outcome"], H.HEAL_PULL_BLOCKED)
        self.assertIsNone(res["stash_ref"])
        self.assertIn("tree restored", res["note"])
        self.assertEqual(calls[-1][len(GIT):],
                         ["stash", "pop", "stash@{0}"])

    def test_blocked_merge_with_stuck_restore_holds(self):
        res, _ = _heal([
            (0, "", ""),
            (0, "Saved\n", ""),
            (0, OWN_STASH, ""),
            (1, "", "error: merge blocked"),
            (1, "", "CONFLICT: needs a hand"),  # restore refused
        ])
        self.assertFalse(res["healed"])
        self.assertEqual(res["outcome"], H.HEAL_STASH_HELD)
        self.assertEqual(res["stash_ref"], "stash@{0}")


class NeverDestructiveTest(unittest.TestCase):
    SCRIPTS = [
        [  # round trip
            (0, "", ""), (0, "Saved\n", ""),
            (0, OWN_STASH, ""),
            (0, "Updating aaa..ooo\n", ""), (0, "ooo\n", ""),
            (0, "Dropped\n", "")],
        [  # held
            (0, "", ""), (0, "Saved\n", ""),
            (0, OWN_STASH, ""),
            (0, "Updating aaa..ooo\n", ""), (0, "ooo\n", ""),
            (1, "", "nope"), (0, OWN_STASH, "")],
        [  # blocked merge + restore
            (0, "", ""), (0, "Saved\n", ""),
            (0, OWN_STASH, ""), (1, "", "nope"),
            (0, "Dropped\n", "")],
    ]

    def test_ladder_argv_has_no_destructive_ops(self):
        # Banned verbs (stash push/pop/list are the only writes);
        # --hard is banned as a flag; drop/clear are banned as
        # stash subverbs (a held entry is never dropped).
        banned_verbs = ("reset", "checkout", "rebase", "push",
                        "clean", "branch")
        for script in self.SCRIPTS:
            _, calls = _heal(list(script))
            for argv in calls:
                tail = argv[len(GIT):]
                self.assertNotIn(tail[0], banned_verbs,
                                 f"banned verb in {tail}")
                self.assertNotIn("--hard", tail,
                                 f"banned --hard in {tail}")
                if tail[0] == "stash":
                    self.assertNotIn(tail[1], ("drop", "clear"),
                                     f"banned stash op in {tail}")
                if tail[0] == "merge":
                    self.assertEqual(tail[1], "--ff-only")


class CountersTest(unittest.TestCase):
    def test_skip_and_heal_counters(self):
        H.reset_counts()
        H.record_skip(H.REASON_DIRTY)
        H.record_skip(H.REASON_DIRTY)
        H.record_skip(H.REASON_LOCK)
        H.record_heal(H.HEAL_ROUND_TRIP)
        H.record_heal(H.HEAL_NOT_APPLICABLE)
        counts = H.get_counts()
        self.assertEqual(counts["skips"][H.REASON_DIRTY], 2)
        self.assertEqual(counts["skips"][H.REASON_LOCK], 1)
        self.assertEqual(counts["heals"][H.HEAL_ROUND_TRIP], 1)
        self.assertEqual(
            counts["heals"][H.HEAL_NOT_APPLICABLE], 1)
        H.reset_counts()
        self.assertEqual(
            H.get_counts(), {"skips": {}, "heals": {}})


class JitterTest(unittest.TestCase):
    def test_backoff_stays_bounded(self):
        for _ in range(50):
            delay = H.jittered_delay(2.0)
            self.assertGreaterEqual(delay, 2.0)
            self.assertLessEqual(delay, 4.0)

    def test_pinned_jitter_is_exact(self):
        with mock.patch.object(H.random, "uniform",
                               return_value=1.5):
            self.assertEqual(H.jittered_delay(2.0), 3.5)


class CauseTest(unittest.TestCase):
    def _cause(self, **entry):
        return H.cause_for({"kind": "webhook", **entry})

    def test_each_reason_names_its_recovery(self):
        self.assertIn("stash or commit",
                      self._cause(
                          skip_reason=H.REASON_DIRTY).lower())
        self.assertIn("rebase",
                      self._cause(
                          skip_reason=H.REASON_DIVERGED).lower())
        self.assertIn("Retry",
                      self._cause(skip_reason=H.REASON_UNRESOLVABLE))
        self.assertIn("remote default",
                      self._cause(skip_reason=H.REASON_NO_DEFAULT))
        self.assertIn("reachable",
                      self._cause(skip_reason=H.REASON_FETCH_FAILED))
        self.assertIn("Retry",
                      self._cause(skip_reason=H.REASON_LOCK))

    def test_held_stash_names_its_ref(self):
        cause = self._cause(
            skip_reason=H.REASON_DIRTY, heal=H.HEAL_STASH_HELD,
            stash_ref="stash@{0}")
        self.assertIn("stash@{0}", cause)
        self.assertIn("retry the pull", cause.lower())

    def test_prior_holding_names_its_ref(self):
        cause = self._cause(
            skip_reason=H.REASON_DIRTY, heal=H.HEAL_PRIOR_HOLDING,
            stash_ref="stash@{1}")
        self.assertIn("stash@{1}", cause)

    def test_legacy_rows_fall_back_to_detail(self):
        self.assertIn(
            "stash or commit",
            self._cause(detail="skipped: tree dirty").lower())
        self.assertIn(
            "rebase",
            self._cause(
                detail="skipped: tree not ff-able").lower())
        self.assertIn(
            "remote default",
            self._cause(detail="skipped: no remote default branch"))

    def test_unknown_rows_still_offer_retry(self):
        self.assertEqual(self._cause(detail="up to date"),
                         "Retry the pull.")

    def test_causes_carry_no_failure_words(self):
        entries = [{}, {"detail": "skipped: tree dirty"},
                   {"detail": "skipped: tree not ff-able"},
                   {"detail": "skipped: no remote default branch"}]
        for reason in H.SKIP_REASONS:
            entries.append({"skip_reason": reason,
                            "detail": "skipped: x"})
        for heal in (H.HEAL_STASH_HELD, H.HEAL_PRIOR_HOLDING,
                     H.HEAL_STASH_FAILED, H.HEAL_PULL_BLOCKED,
                     H.HEAL_NOT_APPLICABLE):
            entries.append({"skip_reason": H.REASON_DIRTY,
                            "heal": heal, "stash_ref": "stash@{0}",
                            "detail": "skipped: tree dirty"})
        for entry in entries:
            self.assertIsNone(
                FAIL_WORDS.search(H.cause_for(entry)),
                H.cause_for(entry))


class SkipReasonLedgerTest(unittest.TestCase):
    def test_dirty_heal_is_ledgered_with_reason(self):
        (ok, detail, advanced, observed), _ = _pull(
            OBSERVE + [
                (0, "", ""),        # merge-base: ff-able
                (0, " M x\n", ""),  # status: dirty
                (0, "", ""),
                (0, "Saved\n", ""),
                (0, OWN_STASH, ""),
                (0, "Updating aaa..ooo\n", ""),
                (0, "ooo\n", ""),
                (0, "Dropped\n", ""),
            ])
        self.assertTrue(ok)
        self.assertEqual(
            detail, "healed: Updating aaa..ooo (stash round-trip)")
        self.assertTrue(advanced)
        self.assertEqual(observed["skip_reason"], H.REASON_DIRTY)
        self.assertEqual(observed["heal"], H.HEAL_ROUND_TRIP)
        counts = H.get_counts()
        self.assertEqual(counts["skips"][H.REASON_DIRTY], 1)
        self.assertEqual(counts["heals"][H.HEAL_ROUND_TRIP], 1)

    def test_dirty_held_skip_carries_stash_ref(self):
        (ok, detail, advanced, observed), _ = _pull(
            OBSERVE + [
                (0, "", ""),
                (0, " M x\n", ""),
                (0, "", ""),
                (0, "Saved\n", ""),
                (0, OWN_STASH, ""),
                (0, "Updating aaa..ooo\n", ""),
                (0, "ooo\n", ""),
                (1, "", "nope"),
                (0, OWN_STASH, ""),
            ])
        self.assertTrue(ok)
        self.assertFalse(advanced)
        self.assertTrue(detail.startswith("skipped: tree dirty"))
        self.assertIn(H.HEAL_STASH_HELD, detail)
        self.assertEqual(observed["skip_reason"], H.REASON_DIRTY)
        self.assertEqual(observed["heal"], H.HEAL_STASH_HELD)
        self.assertEqual(observed["stash_ref"], "stash@{0}")
        self.assertTrue(observed["heal_note"])

    def test_diverged_escalates_with_reason(self):
        (ok, detail, advanced, observed), calls = _pull(
            OBSERVE + [(1, "", "")])  # merge-base: not ff-able
        self.assertTrue(ok)
        self.assertEqual(
            detail,
            "skipped: tree not ff-able \u2014 behind by 3 "
            "(local changes)")
        self.assertFalse(advanced)
        self.assertEqual(observed["skip_reason"], H.REASON_DIVERGED)
        self.assertEqual(observed["heal"], H.HEAL_NOT_APPLICABLE)
        # Escalation mutates nothing: no stash, no merge.
        tails = [c[len(GIT):] for c in calls]
        self.assertNotIn(["stash", "push", mock.ANY], tails)
        for tail in tails:
            self.assertNotEqual(tail[:1], ["stash"])
            self.assertNotEqual(tail[:1], ["merge"])

    def test_unresolvable_ref_escalates_with_reason(self):
        (ok, detail, _, observed), _ = _pull(
            OBSERVE + [(128, "", "fatal: bad revision")])
        self.assertTrue(ok)
        self.assertTrue(detail.startswith("skipped:"))
        self.assertEqual(observed["skip_reason"],
                         H.REASON_UNRESOLVABLE)

    def test_no_default_escalates_with_reason(self):
        (ok, detail, _, observed), _ = _pull([
            (0, "aaa\n", ""),
            (0, "", ""),
            (1, "", "error: No such ref"),
            (128, "", "fatal: Needed a single revision"),
        ])
        self.assertTrue(ok)
        self.assertEqual(detail, "skipped: no remote default branch")
        self.assertEqual(observed["skip_reason"],
                         H.REASON_NO_DEFAULT)

    def test_fetch_network_failure_keeps_cause(self):
        (ok, detail, _, observed), _ = _pull([
            (0, "aaa\n", ""),
            (1, "", "fatal: could not read from remote\n"
                   "network unreachable"),
        ])
        self.assertFalse(ok)
        self.assertEqual(detail, "network unreachable")
        self.assertEqual(observed["skip_reason"],
                         H.REASON_FETCH_FAILED)

    def test_persistent_lock_failure_keeps_cause(self):
        lock_err = ("error: cannot lock ref "
                    "'refs/remotes/origin/main': unable to "
                    "update local ref")
        with mock.patch.object(H.random, "uniform",
                               return_value=0.0):
            (ok, _, _, observed), _ = _pull(
                [(0, "aaa\n", "")] + [(1, "", lock_err)] * 3)
        self.assertFalse(ok)
        self.assertEqual(observed["skip_reason"], H.REASON_LOCK)

    def test_heal_notes_carry_no_failure_words(self):
        scripts = [
            [(0, "", ""), (0, "Saved\n", ""), (0, OWN_STASH, ""),
             (0, "Updating aaa..ooo\n", ""), (0, "ooo\n", ""),
             (0, "Dropped\n", "")],
            [(0, "", ""), (0, "Saved\n", ""), (0, OWN_STASH, ""),
             (0, "Updating aaa..ooo\n", ""), (0, "ooo\n", ""),
             (1, "", "CONFLICT boom"), (0, OWN_STASH, "")],
            [(0, "", ""), (1, "", "error: cannot stash")],
            [(0, "stash@{0}: webhook-heal 1\n", "")],
            [(0, "", ""), (0, "No local changes to save\n", ""),
             (0, "", ""), (0, "Updating aaa..ooo\n", ""),
             (0, "ooo\n", "")],
            [(0, "", ""), (0, "Saved\n", ""), (0, OWN_STASH, ""),
             (1, "", "error: blocked"), (0, "Dropped\n", "")],
        ]
        for script in scripts:
            res, _ = _heal(list(script))
            self.assertIsNone(FAIL_WORDS.search(res["note"]),
                              res["note"])

    def test_worker_ledgers_heal_decision(self):
        orig_pull = N.webhook_pull
        obs = {"ok": True, "detail": "observed", "origin": "ooo",
               "head": "aaa", "lag": 3, "ahead": 0,
               "skip_reason": H.REASON_DIRTY,
               "heal": H.HEAL_STASH_HELD,
               "stash_ref": "stash@{0}",
               "heal_note": "pull applied but the stash needs a hand"}
        detail = ("skipped: tree dirty \u2014 behind by 3 "
                  "(local changes) \u2014 heal stash-held: pull "
                  "applied but the stash needs a hand")
        N.webhook_pull = lambda path: (True, detail, False, obs)
        appended = []
        orig_append = N.ledger.append
        N.ledger.append = appended.append
        try:
            N.webhook_worker("arii/x", "/tmp")
        finally:
            N.webhook_pull = orig_pull
            N.ledger.append = orig_append
        self.assertEqual(len(appended), 1)
        row = appended[0]
        self.assertEqual(row["kind"], "webhook")
        self.assertTrue(row["ok"])
        self.assertFalse(row["advanced"])
        self.assertEqual(row["skip_reason"], H.REASON_DIRTY)
        self.assertEqual(row["heal"], H.HEAL_STASH_HELD)
        self.assertEqual(row["stash_ref"], "stash@{0}")
        self.assertIn("needs a hand", row["heal_note"])
        state = N.webhook_state.get("arii/x", {})
        self.assertEqual(state.get("skip_reason"), H.REASON_DIRTY)
        self.assertEqual(state.get("heal"), H.HEAL_STASH_HELD)


class AutofileGuardTest(unittest.TestCase):
    def _row(self, **over):
        row = {"kind": "webhook", "repo": "remote-server",
               "ok": True, "ts": 1700000000.0,
               "detail": "skipped: tree dirty"}
        row.update(over)
        return row

    def test_skip_with_scary_note_still_not_a_failure(self):
        # Even if a heal note ever carries a failure word, the
        # skip markers keep the row out of autofile.
        row = self._row(
            detail="skipped: tree dirty — heal stash-held: "
                   "unstash conflict needs review",
            skip_reason=H.REASON_DIRTY, heal=H.HEAL_STASH_HELD,
            stash_ref="stash@{0}")
        self.assertIsNone(autofile.failure_signature(row))

    def test_healed_row_is_not_a_failure(self):
        row = self._row(
            detail="healed: Updating aaa..ooo (stash round-trip)",
            skip_reason=H.REASON_DIRTY, heal=H.HEAL_ROUND_TRIP)
        self.assertIsNone(autofile.failure_signature(row))

    def test_legacy_skip_is_not_a_failure(self):
        self.assertIsNone(autofile.failure_signature(self._row()))

    def test_fetch_failure_still_files(self):
        row = self._row(ok=False, detail="network unreachable",
                        skip_reason=H.REASON_FETCH_FAILED)
        self.assertIsNotNone(autofile.failure_signature(row))

    def test_crash_still_files(self):
        row = self._row(ok=False, detail="worker crashed: boom")
        self.assertIsNotNone(autofile.failure_signature(row))


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class RetryEndpointTest(unittest.TestCase):
    def _client(self):
        app = FastAPI()
        app.include_router(notify_router.router)
        return TestClient(app)

    def test_unknown_repo_is_404(self):
        client = self._client()
        resp = client.post(
            "/api/webhook/retry?repo=definitely-not-a-repo-855",
            json={"intent": "", "confirmed": True})
        self.assertEqual(resp.status_code, 404)

    def test_retry_queues_pull_with_counts(self):
        tmp = tempfile.mkdtemp(prefix="retry855-")
        try:
            repo = os.path.basename(tmp)
            old = os.environ.get("BUTTONS_STACK")
            os.environ["BUTTONS_STACK"] = tmp
            N.webhook_pending.discard(repo)
            try:
                client = self._client()
                resp = client.post(
                    f"/api/webhook/retry?repo={repo}",
                    json={"intent": "", "confirmed": True})
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_STACK", None)
                else:
                    os.environ["BUTTONS_STACK"] = old
                N.webhook_pending.discard(repo)
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["queued"])
            self.assertFalse(body["coalesced"])
            self.assertIn("skips", body["skip_counts"])
            self.assertIn("heals", body["skip_counts"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_in_flight_pull_coalesces(self):
        tmp = tempfile.mkdtemp(prefix="retry855-")
        try:
            repo = os.path.basename(tmp)
            old = os.environ.get("BUTTONS_STACK")
            os.environ["BUTTONS_STACK"] = tmp
            N.webhook_pending.add(repo)
            try:
                client = self._client()
                resp = client.post(
                    f"/api/webhook/retry?repo={repo}", json={})
            finally:
                if old is None:
                    os.environ.pop("BUTTONS_STACK", None)
                else:
                    os.environ["BUTTONS_STACK"] = old
                N.webhook_pending.discard(repo)
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertTrue(body["queued"])
            self.assertTrue(body["coalesced"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class AttentionSkipActionTest(unittest.TestCase):
    def _items(self, skips):
        orig = {}
        for name, fn in (
                ("get_workflow_health",
                 lambda repo: {"workflows": []}),
                ("list_failed_jobs", lambda: []),
                ("list_failed_webhooks", lambda: []),
                ("list_skipped_webhooks", lambda: skips),
                ("list_pending_approvals", lambda: []),
                ("ollama_up", lambda: True)):
            orig[name] = getattr(attention, name)
            setattr(attention, name, fn)
        try:
            body = attention.attention("boomtick")
        finally:
            for name, fn in orig.items():
                setattr(attention, name, fn)
        return [i for i in body["items"]
                if i["kind"] == "skipped-webhook"]

    def test_skip_carries_retry_tap_and_cause(self):
        items = self._items([{
            "id": "s1", "ts": 2.0, "ok": True, "advanced": False,
            "repo": "boomtick",
            "detail": "skipped: tree dirty \u2014 behind by 3 "
                      "(local changes)",
            "skip_reason": H.REASON_DIRTY,
            "heal": H.HEAL_STASH_HELD, "stash_ref": "stash@{0}"}])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["severity"], "info")
        self.assertIn("skipped: tree dirty", item["detail"])
        self.assertIn("stash@{0}", item["detail"])
        self.assertEqual(item["action"]["label"], "Retry pull")
        self.assertEqual(item["action"]["method"], "POST")
        self.assertEqual(item["action"]["href"],
                         "api/webhook/retry?repo=boomtick")
        self.assertFalse(item["action"]["destructive"])

    def test_legacy_skip_still_gets_a_cause(self):
        items = self._items([{
            "id": "s1", "ts": 2.0, "ok": True, "advanced": False,
            "repo": "boomtick", "detail": "skipped: tree dirty"}])
        self.assertEqual(len(items), 1)
        self.assertIn("stash or commit", items[0]["detail"].lower())
        self.assertEqual(items[0]["action"]["label"], "Retry pull")


@unittest.skipUnless(shutil.which("git"), "git binary not available")
class RealGitDrillTest(unittest.TestCase):
    """Live drill (DoD): a dirty checkout receiving a webhook pull
    heals with local work preserved, or surfaces exactly one skip
    with its cause — never stacking stashes, never losing bytes."""

    def _git(self, args, cwd):
        env = dict(os.environ)
        env.update({"GIT_AUTHOR_NAME": "lane",
                    "GIT_AUTHOR_EMAIL": "lane@test",
                    "GIT_COMMITTER_NAME": "lane",
                    "GIT_COMMITTER_EMAIL": "lane@test",
                    "GIT_CONFIG_NOSYSTEM": "1"})
        proc = subprocess.run(
            ["git", "-c", "init.defaultBranch=main",
             "-c", "commit.gpgsign=false",
             "-c", f"safe.directory={cwd}", "-C", cwd] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, text=True, env=env)
        self.assertEqual(proc.returncode, 0,
                         f"git {args}: {proc.stderr[-300:]}")
        return proc.stdout.strip()

    def _fixture(self):
        tmp = tempfile.mkdtemp(prefix="heal855-")
        origin = os.path.join(tmp, "origin.git")
        self._git(["init", "--bare", origin], cwd=tmp)
        seed = os.path.join(tmp, "seed")
        self._git(["clone", origin, seed], cwd=tmp)
        self._git(["checkout", "-b", "main"], cwd=seed)
        with open(os.path.join(seed, "file.txt"), "w") as f:
            f.write("v1\n")
        with open(os.path.join(seed, "other.txt"), "w") as f:
            f.write("o1\n")
        self._git(["add", "file.txt", "other.txt"], cwd=seed)
        self._git(["commit", "-m", "v1"], cwd=seed)
        self._git(["push", "-u", "origin", "main"], cwd=seed)
        work = os.path.join(tmp, "work")
        self._git(["clone", "-b", "main", origin, work], cwd=tmp)
        return tmp, seed, work

    def _stash_list(self, work):
        return self._git(["stash", "list"], cwd=work)

    def test_dirty_checkout_heals_and_preserves_work(self):
        tmp, seed, work = self._fixture()
        try:
            with open(os.path.join(seed, "file.txt"), "a") as f:
                f.write("v2\n")
            self._git(["commit", "-am", "v2"], cwd=seed)
            self._git(["push", "origin", "main"], cwd=seed)
            v2 = self._git(["rev-parse", "HEAD"], cwd=seed)
            target = os.path.join(work, "other.txt")
            with open(target, "a") as f:
                f.write("local-dirt\n")
            ok, detail, advanced, observed = N.webhook_pull(work)
            self.assertTrue(ok)
            self.assertTrue(advanced)
            self.assertTrue(detail.startswith("healed:"))
            self.assertEqual(observed["skip_reason"],
                             H.REASON_DIRTY)
            self.assertEqual(observed["heal"], H.HEAL_ROUND_TRIP)
            self.assertEqual(
                self._git(["rev-parse", "HEAD"], cwd=work), v2)
            with open(target) as f:
                self.assertIn("local-dirt\n", f.read())
            with open(os.path.join(work, "file.txt")) as f:
                self.assertIn("v2\n", f.read())
            self.assertEqual(self._stash_list(work), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_conflicting_dirt_holds_and_surfaces_once(self):
        tmp, seed, work = self._fixture()
        try:
            with open(os.path.join(seed, "file.txt"), "w") as f:
                f.write("v2\n")
            self._git(["commit", "-am", "v2"], cwd=seed)
            self._git(["push", "origin", "main"], cwd=seed)
            target = os.path.join(work, "file.txt")
            with open(target, "w") as f:
                f.write("local\n")
            ok, detail, advanced, observed = N.webhook_pull(work)
            self.assertTrue(ok)
            self.assertFalse(advanced)
            self.assertTrue(
                detail.startswith("skipped: tree dirty"))
            self.assertIn(H.HEAL_STASH_HELD, detail)
            self.assertEqual(observed["stash_ref"], "stash@{0}")
            stashes = self._stash_list(work)
            self.assertIn(H.STASH_MARKER, stashes)
            # A second webhook neither stacks a stash nor loses the
            # work: it surfaces the same single held skip.
            ok2, detail2, _, observed2 = N.webhook_pull(work)
            self.assertTrue(ok2)
            self.assertTrue(
                detail2.startswith("skipped: tree dirty"))
            self.assertEqual(observed2["heal"],
                             H.HEAL_PRIOR_HOLDING)
            self.assertEqual(len(self._stash_list(work)
                                 .splitlines()), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_retry_after_resolve_heals(self):
        tmp, seed, work = self._fixture()
        try:
            with open(os.path.join(seed, "file.txt"), "w") as f:
                f.write("v2\n")
            self._git(["commit", "-am", "v2"], cwd=seed)
            self._git(["push", "origin", "main"], cwd=seed)
            v2 = self._git(["rev-parse", "HEAD"], cwd=seed)
            target = os.path.join(work, "file.txt")
            with open(target, "w") as f:
                f.write("local\n")
            ok, _, _, _ = N.webhook_pull(work)
            self.assertTrue(ok)
            # Operator resolves: merged content wins, the held stash
            # (whose bytes now live in the tree) is dropped.
            with open(target, "w") as f:
                f.write("v2\nlocal\n")
            self._git(["add", "file.txt"], cwd=work)
            self._git(["stash", "drop"], cwd=work)
            ok, detail, _, _ = N.webhook_pull(work)
            self.assertTrue(ok)
            self.assertTrue(detail.startswith("healed:"))
            self.assertEqual(
                self._git(["rev-parse", "HEAD"], cwd=work), v2)
            with open(target) as f:
                self.assertEqual(f.read(), "v2\nlocal\n")
            self.assertEqual(self._stash_list(work), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
