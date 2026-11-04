"""Webhook-pull healing ladder (#855).

`backend.notify.webhook_pull` used to skip dirty/diverged checkouts
forever: every push re-observed the same lag, the checkout never
moved, and the only recovery was hand-fixing git state. This module
owns the heal-or-escalate ladder so no skipped pull rots silently:

- diagnose first: every skip carries a machine `skip_reason`
  (dirty-tree, diverged, unresolvable-ref, no-remote-default,
  fetch-failed, lock-contention) with per-reason counters;
- self-heal where it is safe: dirty trees take the stash-and-pull
  rung (stash, fast-forward, unstash-or-hold); every other reason
  escalates with its precise cause — there is no safe automatic
  primitive for diverged trees (a rebase would rewrite local
  commits) or vanished refs;
- never destructive: the ladder runs no reset --hard, no checkout,
  no branch deletes, no stash drop/clear, no rebase, no push. A
  stash whose pop fails stays in the stash with its ref ledgered.

Ledger entries carry skip_reason/heal/stash_ref/heal_note (set by
the pull path, read by the console). Heal notes avoid failure words
(fail/error/conflict/...) so skip rows never trip the autofile
failure regexes — autofile stays out of this path by construction,
with an explicit guard in backend/autofile.py as backstop.
"""

from __future__ import annotations

import random
import subprocess
import threading
import time

REASON_DIRTY = "dirty-tree"
REASON_DIVERGED = "diverged"
REASON_UNRESOLVABLE = "unresolvable-ref"
REASON_NO_DEFAULT = "no-remote-default"
REASON_FETCH_FAILED = "fetch-failed"
REASON_LOCK = "lock-contention"

SKIP_REASONS = (
    REASON_DIRTY,
    REASON_DIVERGED,
    REASON_UNRESOLVABLE,
    REASON_NO_DEFAULT,
    REASON_FETCH_FAILED,
    REASON_LOCK,
)

# Heal outcomes. "not-applicable" means the ladder considered the
# reason and found no safe automatic rung — the skip escalates with
# its cause instead of healing.
HEAL_ROUND_TRIP = "stash-pull-pop"
HEAL_PULL_THROUGH = "pull-through"
HEAL_STASH_HELD = "stash-held"
HEAL_STASH_FAILED = "stash-failed"
HEAL_PULL_BLOCKED = "pull-blocked"
HEAL_PRIOR_HOLDING = "prior-holding"
HEAL_NOT_APPLICABLE = "not-applicable"

# Marker embedded in every stash message this ladder creates, so a
# retry never stacks a second stash on a held one — the earlier heal
# stash is found and the pull escalates instead.
STASH_MARKER = "webhook-heal"

_counts_lock = threading.Lock()
_skip_counts: dict = {}
_heal_counts: dict = {}


def record_skip(reason: str) -> None:
    """Count one diagnosed skip (unknown reasons pass through)."""
    with _counts_lock:
        _skip_counts[reason] = _skip_counts.get(reason, 0) + 1


def record_heal(outcome: str) -> None:
    """Count one ladder outcome (healed or escalated)."""
    with _counts_lock:
        _heal_counts[outcome] = _heal_counts.get(outcome, 0) + 1


def get_counts() -> dict:
    """Snapshot of per-reason skip and per-outcome heal counters."""
    with _counts_lock:
        return {"skips": dict(_skip_counts),
                "heals": dict(_heal_counts)}


def reset_counts() -> None:
    """Zero all counters (tests only)."""
    with _counts_lock:
        _skip_counts.clear()
        _heal_counts.clear()


def jittered_delay(base: float) -> float:
    """Lock-retry backoff: base plus uniform jitter in [0, base].

    Racing git ops (the terminal container shares the checkout) wake
    together; the jitter keeps retries from re-colliding.
    """
    return base + random.uniform(0, base)


def _proc_ok(proc) -> bool:
    return getattr(proc, "returncode", 1) == 0


def list_heal_stashes(git, env, run=None) -> list:
    """Refs (newest first) of webhook-heal stashes still present."""
    run = run or subprocess.run
    try:
        proc = run(
            git + ["stash", "list", "--format=%gd:%gs"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True, env=env)
    except Exception:
        return []
    if not _proc_ok(proc):
        return []
    refs = []
    for line in (proc.stdout or "").splitlines():
        ref, _, subject = line.partition(":")
        if STASH_MARKER in subject:
            refs.append(ref.strip())
    return refs


def _find_own_stash(git, env, run):
    """Ref of this attempt's stash entry, or None.

    The ladder refuses to start while any heal stash is present,
    so the first marked entry found afterwards is ours. Untracked-
    only dirt creates no entry (`stash push` without -u leaves
    untracked files in place and reports nothing to save) — that
    reads as None and the pull-through rung applies.
    """
    found = list_heal_stashes(git, env, run)
    return found[0] if found else None


def _last_line(text: str, fallback: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else fallback


def heal_dirty_tree(git, env, target, old_head, run=None) -> dict:
    """Stash-and-pull rung for a dirty tree.

    Returns {attempted, healed, advanced, outcome, note, stash_ref,
    merge_line}. attempted is always True (calling it IS the
    attempt). healed+advanced mirrors the pull contract: advanced
    means the checkout moved AND the tree is whole — a held stash
    reports advanced False so the skip stays operator-visible until
    a later pull truly resolves it.
    """
    run = run or subprocess.run
    stamp = str(int(time.time()))
    holding = list_heal_stashes(git, env, run)
    if holding:
        return {
            "attempted": True, "healed": False, "advanced": False,
            "outcome": HEAL_PRIOR_HOLDING, "stash_ref": holding[0],
            "merge_line": "",
            "note": (f"earlier heal stash {holding[0]} still "
                     "present; resolve it, then retry the pull"),
        }
    # Heal notes use a fixed vocabulary and never quote raw git
    # output: quoted stderr ("error: ...", "CONFLICT ...") would
    # trip the History/autofile failure-word regexes and turn a
    # nudge into a failure. The outcome code + fixed note name the
    # precise recovery step, which is what the operator needs.
    try:
        pushed = run(
            git + ["stash", "push", "-m",
                   f"{STASH_MARKER} {stamp}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, text=True, env=env)
    except Exception:
        pushed = None
    if pushed is None or not _proc_ok(pushed):
        return {
            "attempted": True, "healed": False, "advanced": False,
            "outcome": HEAL_STASH_FAILED, "stash_ref": None,
            "merge_line": "",
            "note": ("local changes could not be stashed; stash "
                     "or commit by hand, then retry the pull"),
        }
    own = _find_own_stash(git, env, run)
    try:
        merge = run(
            git + ["merge", "--ff-only", target],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, text=True, env=env)
    except Exception:
        merge = None
    merge_line = _last_line(
        merge.stdout if merge is not None else "",
        "up to date")
    if merge is None or not _proc_ok(merge):
        # Pull blocked after the stash: restore the tree first so
        # user work is never left stashed silently, then escalate.
        restored_note = ""
        if own:
            try:
                back = run(
                    git + ["stash", "pop", own],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=120, text=True, env=env)
            except Exception:
                back = None
            if back is not None and _proc_ok(back):
                restored_note = "tree restored; "
            else:
                return {
                    "attempted": True, "healed": False,
                    "advanced": False,
                    "outcome": HEAL_STASH_HELD, "stash_ref": own,
                    "merge_line": merge_line,
                    "note": (f"pull stayed blocked and the stash "
                             f"({own}) needs a hand; resolve it, "
                             "then retry the pull"),
                }
        return {
            "attempted": True, "healed": False, "advanced": False,
            "outcome": HEAL_PULL_BLOCKED, "stash_ref": None,
            "merge_line": merge_line,
            "note": (f"{restored_note}pull stayed blocked after "
                     "the stash; rebase local work onto "
                     f"{target}, then retry the pull"),
        }
    try:
        new_head = run(
            git + ["rev-parse", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, text=True, env=env).stdout.strip() or None
    except Exception:
        new_head = None
    advanced = bool(old_head and new_head
                    and old_head != new_head)
    if not own:
        # Untracked-only dirt: nothing was stashed, the fast-forward
        # applied past the untracked files, nothing to restore.
        return {
            "attempted": True, "healed": True, "advanced": advanced,
            "outcome": HEAL_PULL_THROUGH, "stash_ref": None,
            "merge_line": merge_line,
            "note": ("untracked files left in place; pull "
                     "applied"),
        }
    try:
        pop = run(
            git + ["stash", "pop", own],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, text=True, env=env)
    except Exception:
        pop = None
    if pop is not None and _proc_ok(pop):
        return {
            "attempted": True, "healed": True, "advanced": advanced,
            "outcome": HEAL_ROUND_TRIP, "stash_ref": None,
            "merge_line": merge_line,
            "note": ("local changes stashed, pull applied, stash "
                     "restored"),
        }
    # Pop refused (git keeps the entry): the pull applied but the
    # tree needs a hand. advanced stays False so the skip remains
    # operator-visible until a later pull truly resolves it.
    still = _find_own_stash(git, env, run) or own
    return {
        "attempted": True, "healed": False, "advanced": False,
        "outcome": HEAL_STASH_HELD, "stash_ref": still,
        "merge_line": merge_line,
        "note": (f"pull applied but the stash ({still}) needs a "
                 "hand; resolve it, then retry the pull"),
    }


# Recovery sentence per skip reason (the precise operator step;
# legacy rows without a reason fall back to detail parsing below).
_RECOVERIES = {
    REASON_DIRTY: "stash or commit the local changes, then "
                  "retry the pull",
    REASON_DIVERGED: "rebase the local commits onto the remote "
                     "default, then retry the pull",
    REASON_UNRESOLVABLE: "retry the pull",
    REASON_NO_DEFAULT: "set the remote default branch, then "
                       "retry the pull",
    REASON_FETCH_FAILED: "retry when the remote is reachable",
    REASON_LOCK: "retry the pull",
}

_CAUSES = {
    REASON_DIRTY: "Local changes block the pull.",
    REASON_DIVERGED: "Local commits diverge from the remote "
                     "default.",
    REASON_UNRESOLVABLE: "The remote ref vanished mid-pull.",
    REASON_NO_DEFAULT: "No remote default branch resolved.",
    REASON_FETCH_FAILED: "The fetch did not complete.",
    REASON_LOCK: "A racing git op held the ref lock.",
}


def _reason_from_detail(detail: str):
    """Reason for legacy rows that predate skip_reason."""
    text = (detail or "").lower()
    if "tree dirty" in text or "local changes" in text:
        return REASON_DIRTY
    if "not ff-able" in text:
        return REASON_DIVERGED
    if "no remote default" in text:
        return REASON_NO_DEFAULT
    if "fetch" in text:
        return REASON_FETCH_FAILED
    return None


def cause_for(entry: dict) -> str:
    """One inline cause + recovery sentence for a skip row.

    Reason-aware: a held stash names its ref, a diverged tree names
    the rebase, a lock race just asks for a retry. Legacy rows
    without skip_reason infer the reason from the detail text.
    """
    entry = entry or {}
    reason = entry.get("skip_reason") or _reason_from_detail(
        str(entry.get("detail", "") or ""))
    heal = entry.get("heal") or ""
    ref = entry.get("stash_ref") or ""
    if heal == HEAL_STASH_HELD and ref:
        return (f"Pull applied; the stash ({ref}) needs a hand — "
                "resolve it, then retry the pull.")
    if heal == HEAL_PRIOR_HOLDING and ref:
        return (f"An earlier heal stash ({ref}) is still present "
                "— resolve it, then retry the pull.")
    if heal == HEAL_STASH_FAILED:
        return ("The stash did not land — stash or commit by hand, "
                "then retry the pull.")
    if heal == HEAL_PULL_BLOCKED:
        return ("The pull stayed blocked after the stash — rebase "
                "local work onto the remote default, then retry "
                "the pull.")
    if reason in _CAUSES:
        recovery = _RECOVERIES[reason]
        recovery = recovery[0].upper() + recovery[1:]
        return f"{_CAUSES[reason]} {recovery}."
    return "Retry the pull."
