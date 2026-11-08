"""Webhook auto-pull helpers (HMAC verify, observe/pull/worker)."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import subprocess
import threading
import time

from backend import ledger
from backend import webhook_heal

WEBHOOK_MAX_BODY = 10_000_000

webhook_state: dict = {}  # repo -> {"at": epoch, "ok": bool,
#   "detail": str, "advanced": bool, "observed": {...}}
webhook_state_lock = threading.Lock()
webhook_pull_lock = threading.Lock()  # serializes pulls, not status reads
webhook_pending: set = set()


def webhook_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "")


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expect = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expect, header)


def _git_base(path):
    """git argv prefix for the pull path: quarantined hooks, explicit
    safe.directory. Observe/advance only — never rebuilds, never
    installs deps, never runs hooks (core.hooksPath=/dev/null)."""
    return ["git", "-c", f"safe.directory={path}", "-C", path,
            "-c", "core.hooksPath=/dev/null"]


def _webhook_target(git, env):
    """Resolve the pull target: the repo's actual remote default
    branch (origin/HEAD), falling back to origin/main. Returns e.g.
    "origin/leader", or None when no remote default resolves — never
    raises. Candidates are verified so a stale origin/HEAD pointing
    at a deleted branch does not become the target."""
    try:
        head = subprocess.run(
            git + ["symbolic-ref", "refs/remotes/origin/HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True, env=env)
    except Exception:
        head = None
    candidates = []
    if head is not None and head.returncode == 0:
        ref = (head.stdout or "").strip()
        if ref.startswith("refs/remotes/"):
            candidates.append(ref[len("refs/remotes/"):])
    candidates.append("origin/main")
    for candidate in candidates:
        try:
            verify = subprocess.run(
                git + ["rev-parse", "--verify", candidate],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env)
        except Exception:
            continue
        if verify.returncode == 0:
            return candidate
    return None


# Ref-lock races (#406): the webhook pull shares its checkout with
# the terminal container's git ops, so `git fetch origin` can fail
# with a transient lock error ("unable to update local ref") while
# another process holds the ref lock. Each failed fetch ledgers a
# genuine ok:false webhook row, and every push regroups them into
# "same failure repeated" incidents. The fetch below retries lock
# contention only — never network/auth errors (retrying those would
# mask the cause, per the issue's rejected alternatives).
WEBHOOK_FETCH_RETRIES = 2
WEBHOOK_FETCH_RETRY_DELAY = 2.0

_REF_LOCK_RES = (
    re.compile(r"unable to update local ref", re.IGNORECASE),
    re.compile(r"cannot lock ref", re.IGNORECASE),
    re.compile(r"cannot store ref", re.IGNORECASE),
    re.compile(r"\.lock['\"]?\s*(file)?\s*exists", re.IGNORECASE),
    re.compile(r"unable to create .*\.lock", re.IGNORECASE),
)


def _fetch_lock_contended(stderr: str) -> bool:
    """True when a failed fetch's stderr shows ref-lock contention
    (another git process holds the lock) rather than a real
    failure. Only these transient races are retried."""
    return any(rx.search(stderr or "") for rx in _REF_LOCK_RES)


def _fetch_origin(git, env):
    """`git fetch origin` with ref-lock backoff (#406, jitter #855):
    a fetch that fails on lock contention sleeps briefly and
    retries (up to WEBHOOK_FETCH_RETRIES), since the lock holder is
    usually a seconds-long racing git op. The sleep carries uniform
    jitter so racing git ops do not re-collide. Returns the final
    CompletedProcess — success, or the last attempt's failure for
    the caller to report exactly as before."""
    fetch = None
    for attempt in range(WEBHOOK_FETCH_RETRIES + 1):
        fetch = subprocess.run(
            git + ["fetch", "origin"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120, text=True, env=env)
        if fetch.returncode == 0:
            return fetch
        if attempt < WEBHOOK_FETCH_RETRIES \
                and _fetch_lock_contended(fetch.stderr):
            time.sleep(webhook_heal.jittered_delay(
                WEBHOOK_FETCH_RETRY_DELAY))
            continue
        return fetch
    return fetch


def webhook_observe(path):
    """Observe-only remote state: fetch origin, record the remote
    default branch + HEAD positions and lag. Never writes to the
    tree: no merge, no checkout, no stash, no reset — safe on
    dirty/diverged checkouts.

    Returns {"ok", "detail", "origin", "head", "lag", "ahead",
    "target"}: target is the resolved pull target (e.g.
    "origin/leader"), lag is commits behind it (HEAD..target),
    ahead is local-only commits (target..HEAD). ok=False means the
    fetch itself failed and no remote state was observed. When the
    fetch succeeds but no remote default resolves, ok is True with
    target None — the pull quiet-skips, never FAILED.
    """
    git = _git_base(path)
    env = dict(os.environ)
    observed = {"ok": False, "detail": "", "origin": None,
                "head": None, "lag": None, "ahead": None,
                "target": None}
    try:
        observed["head"] = subprocess.run(
            git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, text=True,
            env=env).stdout.strip() or None
    except Exception:
        observed["head"] = None
    fetch = _fetch_origin(git, env)
    if fetch.returncode != 0:
        observed["detail"] = (
            (fetch.stderr or "").strip().splitlines()[-1]
            if (fetch.stderr or "").strip() else "fetch failed")
        # Full-stderr classification (#855): the detail keeps only
        # the last line, but the lock race may sit earlier.
        observed["lock_contended"] = _fetch_lock_contended(
            fetch.stderr or "")
        return observed
    target = _webhook_target(git, env)
    observed["target"] = target
    if not target:
        observed["ok"] = True
        observed["detail"] = "no remote default branch"
        return observed
    try:
        observed["origin"] = subprocess.run(
            git + ["rev-parse", target],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True, env=env).stdout.strip() or None
    except Exception:
        observed["origin"] = None
    for key, rev_range in (("lag", f"HEAD..{target}"),
                           ("ahead", f"{target}..HEAD")):
        try:
            out = subprocess.run(
                git + ["rev-list", "--count", rev_range],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env).stdout.strip()
            observed[key] = int(out)
        except Exception:
            observed[key] = None
    observed["ok"] = True
    observed["detail"] = "observed"
    return observed


def _skip_detail(reason, observed):
    """Skip detail with fresh lag: 'skipped: <reason> — behind by N
    (local changes)'. Falls back to the bare reason when lag is
    unknown. Always starts with 'skipped' (attention matcher)."""
    lag = (observed or {}).get("lag")
    if isinstance(lag, bool) or not isinstance(lag, int):
        return f"skipped: {reason}"
    return f"skipped: {reason} \u2014 behind by {lag} (local changes)"


def _heal_or_skip_dirty(git, env, target, old_head, observed):
    """Dirty-tree rung (#855): stash-and-pull, unstash-or-hold.

    A dirty tree no longer skips quietly: the ladder stashes local
    work, fast-forwards, and restores the stash. When the stash
    cannot be restored the entry is kept (never dropped) and the
    pull skips with the stash ref + cause ledgered. Returns the
    webhook_pull (ok, detail, advanced, observed) tuple.
    """
    res = webhook_heal.heal_dirty_tree(git, env, target, old_head)
    webhook_heal.record_skip(webhook_heal.REASON_DIRTY)
    webhook_heal.record_heal(res["outcome"])
    observed["skip_reason"] = webhook_heal.REASON_DIRTY
    observed["heal"] = res["outcome"]
    if res.get("stash_ref"):
        observed["stash_ref"] = res["stash_ref"]
    if res.get("note"):
        observed["heal_note"] = res["note"]
    if res["healed"]:
        how = "stash round-trip" \
            if res["outcome"] == webhook_heal.HEAL_ROUND_TRIP \
            else "untracked files kept"
        return True, \
            f"healed: {res['merge_line']} ({how})"[:200], \
            res["advanced"], observed
    base = _skip_detail("tree dirty", observed)
    return True, \
        f"{base} \u2014 heal {res['outcome']}: {res['note']}"[:200], \
        False, observed


def webhook_pull(path):
    """Pull-only auto-pull: observe remote state, then fast-forward.

    Observe (fetch + record the remote default branch and HEAD
    positions + lag) always runs, even on dirty/diverged trees.
    Clean trees advance via merge --ff-only when HEAD is an
    ancestor of the remote default branch; dirty trees heal via
    the stash-and-pull ladder (#855), every other skip escalates
    with its cause. The ladder never runs reset --hard, checkout,
    rebase, or stash drop/clear.

    Never rebuilds, never installs deps, never runs hooks
    (core.hooksPath=/dev/null). Returns (ok, detail, advanced,
    observed): observed is the webhook_observe dict — remote state
    fresh even when the local tree did not move — plus skip_reason
    on every diagnosed skip, and heal/stash_ref/heal_note when the
    ladder ran.
    """
    git = _git_base(path)
    env = dict(os.environ)
    observed = {"ok": False, "detail": "", "origin": None,
                "head": None, "lag": None, "ahead": None,
                "target": None}
    with webhook_pull_lock:
        try:
            observed = webhook_observe(path)
            if not observed.get("ok"):
                reason = webhook_heal.REASON_LOCK \
                    if observed.get("lock_contended") \
                    else webhook_heal.REASON_FETCH_FAILED
                observed["skip_reason"] = reason
                webhook_heal.record_skip(reason)
                raise RuntimeError(
                    observed.get("detail") or "fetch failed")
            target = observed.get("target")
            if not target:
                # No remote default branch (e.g. non-main repo with
                # no resolvable target): quiet skip, never FAILED.
                observed["skip_reason"] = \
                    webhook_heal.REASON_NO_DEFAULT
                observed["heal"] = \
                    webhook_heal.HEAL_NOT_APPLICABLE
                webhook_heal.record_skip(
                    webhook_heal.REASON_NO_DEFAULT)
                webhook_heal.record_heal(
                    webhook_heal.HEAL_NOT_APPLICABLE)
                return True, "skipped: no remote default branch", \
                    False, observed
            old_head = observed.get("head")
            ancestor = subprocess.run(
                git + ["merge-base", "--is-ancestor",
                       "HEAD", target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env)
            if ancestor.returncode in (1, 128):
                # Mid-work checkout (1) or unresolvable ref (128):
                # quiet skip, not a failure alert. Remote state was
                # still observed (fetch + lag above). No safe
                # automatic rung exists — a rebase would rewrite
                # local commits — so the ladder escalates with the
                # reason instead of healing.
                reason = webhook_heal.REASON_DIVERGED \
                    if ancestor.returncode == 1 \
                    else webhook_heal.REASON_UNRESOLVABLE
                observed["skip_reason"] = reason
                observed["heal"] = \
                    webhook_heal.HEAL_NOT_APPLICABLE
                webhook_heal.record_skip(reason)
                webhook_heal.record_heal(
                    webhook_heal.HEAL_NOT_APPLICABLE)
                return True, _skip_detail(
                    "tree not ff-able", observed)[:200], False, observed
            tree = subprocess.run(
                git + ["status", "--porcelain"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, text=True, env=env)
            if (tree.stdout or "").strip():
                return _heal_or_skip_dirty(
                    git, env, target, old_head, observed)
            merge = subprocess.run(
                git + ["merge", "--ff-only", target],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120, text=True, env=env)
            if merge.returncode != 0:
                raise RuntimeError(
                    (merge.stderr or "").strip().splitlines()[-1]
                    if merge.stderr.strip() else "merge failed")
            out = (merge.stdout or "").strip().splitlines()
            ok, detail = True, (out[-1] if out else "up to date")[:200]
            try:
                new_head = subprocess.run(
                    git + ["rev-parse", "HEAD"], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=30, text=True,
                    env=env).stdout.strip() or None
            except Exception:
                new_head = None
            advanced = bool(old_head and new_head
                            and old_head != new_head)
        except Exception as exc:
            ok, detail, advanced = False, str(exc)[:200], False
    return ok, detail, advanced, observed


def webhook_worker(repo, path):
    try:
        ok, detail, advanced, observed = webhook_pull(path)
    except Exception as exc:  # never stick pending: coalesce would
        ok, detail, advanced = False, f"worker crashed: {exc}", False
        observed = None
    # Every heal/skip decision is ledgered (#855): the machine
    # reason + ladder outcome ride top-level so the console reads
    # them without parsing detail text. Crashes carry no reason —
    # they are genuine failures, still autofile-eligible.
    extras = {}
    if isinstance(observed, dict):
        for key in ("skip_reason", "heal", "stash_ref"):
            value = observed.get(key)
            if value:
                extras[key] = value
        note = observed.get("heal_note")
        if note:
            extras["heal_note"] = str(note)[:300]
    with webhook_state_lock:
        webhook_state[repo] = {"at": time.time(), "ok": ok,
                               "detail": detail, "advanced": advanced,
                               "observed": observed, **extras}
        webhook_pending.discard(repo)
    # Every outcome hits the ledger: successes resolve the attention
    # incidents that failures raise. Skips carry fresh observed
    # positions + lag so lagging checkouts never go stale. append()
    # never raises.
    ledger.append({"kind": "webhook", "repo": repo, "ok": ok,
                   "advanced": advanced, "detail": detail[:200],
                   "observed": observed, **extras})
    if ok:
        # A successful pull may have moved the checkout: cached
        # inbox/prs lists recompute on next read (#888). Failures
        # leave the checkout unchanged, so the cache stands. Lazy
        # router imports: backend.notify sits below the routers.
        # Worker repo may be full (webhook) or short (retry tap).
        from backend.api.routers import inbox as inbox_mod
        from backend.api.routers import pulls as pulls_mod
        short = str(repo or "").split("/")[-1].strip()
        if short:
            pulls_mod.invalidate_prs_cache(short)
            inbox_mod.invalidate_inbox_cache(short)
    print(f"webhook pull {repo}: {'ok' if ok else 'FAILED'}: {detail}",
          flush=True)
