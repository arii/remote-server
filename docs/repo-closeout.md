# Repository closeout

The queue runner already sweeps worktrees at startup and after successful worker
closeout. That does not cover arbitrary GitHub merges or unrelated coding sessions.
Interactive post-merge hooks are not a general closeout trigger either.

`python3 scripts/repo-closeout.py REPO...` reports the current state.
Add `--apply` to fast-forward clean main checkouts with deployment hooks disabled
and remove only clean landed linked worktrees. Dirty, untracked and ignored files,
active branches, detached HEADs, main/master worktrees and locked worktrees stay.
Unknown files are never staged or committed. Branch names alone do not prove a
squash merge: the merged PR head must match. A failed fetch stops deletion.

For recurring closeout, run `CLOSEOUT_USER=OWNER sudo -E bash
scripts/install-repo-closeout-cron.sh` on the managed host. The installer checks
cron/Python availability, exact checkout ownership and an explicit four-repo list.
It refuses to overwrite an existing schedule. It does not install packages,
restart services or create a detached background process. Inspect the generated
`/etc/cron.d/repo-closeout`, run once manually as the owner and verify the next
scheduled result in `/tmp/repo-closeout-OWNER.log` before calling it installed.
The log is an operational status record, not an authorization to commit work.

If only the minimal code-server container is reachable, installation is blocked:
its filesystem is not proof of a managed host scheduler. Keep this distinction
visible rather than claiming that a merged script is running automation.

Tests: `python3 -m unittest discover -s tests -p test_repo_closeout.py -v`.
