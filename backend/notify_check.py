"""Scheduled notification check runner (#822).

One repo per invocation: fetches that repo's failing runs + open PR
merge states, runs them through
backend.notify.check_notifications_and_digest (ci_fail/conflict
pushes + interval digest, state-deduped, #532 no-stamp-on-failure),
and prints a short audit trail to stdout (the jobs run output).

Invoked by the engine's `check-notifications` action step; never
imported by the API (steps are subprocesses, not in-process calls).
Exit 0 whenever the check completed — even when gh degraded (the
repo is then skipped as unknown, keeping its stored keys) — so a
transient gh outage fails open without failing the run. Unexpected
crashes exit nonzero via the traceback.
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one repo's notification check.")
    parser.add_argument("--repo", default="remote-server",
                        help="repo basename (state key)")
    parser.add_argument("--gh", default="",
                        help="gh owner/repo path "
                             "(default: arii/<repo>)")
    args = parser.parse_args(argv)

    from backend.notify import check_notifications_and_digest, \
        collect_notify_brief, send_notification

    gh = args.gh.strip() or f"arii/{args.repo.strip()}"
    repo_name = args.repo.strip() or "remote-server"

    def fetcher():
        return {repo_name: collect_notify_brief(repo_name, gh)}

    sent: list = []

    def counting_sender(**kw):
        res = send_notification(**kw)
        sent.append(kw)
        title = kw.get("title", "")
        print(f"push [{kw.get('event_type', '?')}] {title}", flush=True)
        return res

    try:
        state = check_notifications_and_digest(
            fetcher=fetcher, sender=counting_sender)
    except Exception as exc:
        # Never fail silently (#1125): the scheduler merges stderr
        # into the run record, but a stdout summary keeps the
        # failure visible to stdout-only consumers too. Re-raise so
        # the traceback (and nonzero exit) still lands as well.
        print(f"notify-check repo={repo_name} failed: {exc}", flush=True)
        raise
    ci_keys = len((state.get("ci") or {}).get(repo_name, {}))
    conflict_keys = len(
        (state.get("conflicts") or {}).get(repo_name, {}))
    print(f"notify-check repo={repo_name} ci_tracked={ci_keys} "
          f"conflicts_tracked={conflict_keys} pushes={len(sent)} "
          f"last_digest={state.get('last_digest', 0)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
