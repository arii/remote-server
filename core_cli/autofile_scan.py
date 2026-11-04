"""Scheduled repeated-failure autofile runner (#825).

One repo per invocation: scans that repo's ledger rows for failure
signatures crossing the threshold (>=3 same-signature failures in
24h, mirroring the historyUtils collapse rule), files each crossed
signature once through the create-issue path, and skips signatures
whose marker issue is still open. Prints a short audit trail to
stdout (the jobs run output).

Invoked by the engine's `autofile-scan` action step; never imported
by the API (steps are subprocesses, not in-process calls). Exit 0
whenever the scan completed - even when every signature errored
(the errors are recorded in the audit trail, and the next daily
tick retries) - so a transient gh outage fails closed without
failing the run. Unexpected crashes exit nonzero via the traceback.
"""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one repo's repeated-failure autofile scan.")
    parser.add_argument("--repo", default="remote-server",
                        help="repo basename (ledger filter + file "
                             "target)")
    parser.add_argument("--gh", default="",
                        help="gh owner/repo path "
                             "(default: arii/<repo>)")
    args = parser.parse_args(argv)

    from backend.autofile import scan_repo

    gh = args.gh.strip() or f"arii/{args.repo.strip()}"
    repo_name = args.repo.strip() or "remote-server"

    result = scan_repo(repo_name, gh=gh)
    for row in result["filed"]:
        print(f"filed [{row['signature']}] count={row['count']} "
              f"{row['url']}", flush=True)
    for row in result["skipped"]:
        print(f"skip [{row['signature']}] count={row['count']} "
              f"marker issue open", flush=True)
    for row in result["errors"]:
        print(f"error [{row['signature']}] {row['error']}",
              flush=True)
    print(f"autofile-scan repo={repo_name} "
          f"scanned={result['scanned']} "
          f"signatures={result['signatures']} "
          f"filed={len(result['filed'])} "
          f"skipped={len(result['skipped'])} "
          f"errors={len(result['errors'])}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
