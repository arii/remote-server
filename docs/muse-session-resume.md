# Resume Muse sessions after a crash or terminal restart

Muse session logs live under `/root/.local/share/muse/sessions/`, which persists
across terminal container recreates via the `local-data` volume (`/root/.config/muse`
persists via `muse-data`). A crash or restart does not delete sessions — just
reattach to one.

## Automatic offer

On interactive shell start, the shell offers to resume when the latest
session for the current workspace was interrupted by a restart
(segment unterminated, updated before container boot, recent):

- `Enter` or `Y` resumes: `muse resume <session-uuid>` (resume-only,
  never anything destructive).
- Anything else declines and stops asking for that session until the
  next restart.
- No answer within 10 seconds resumes automatically.
- Interrupt, error, or missing pieces always land on a fresh shell.

Opt out entirely: `MUSE_NO_AUTORESUME=1`. Implementation:
`terminal/bin/muse-resume-offer` (decision, tested in
`tests/test_resume_offer.py`) + `terminal/bin/muse-resume-ask`
(prompt), hooked from `/root/.bashrc` by `terminal/Dockerfile`.

## Quick resume

From the repo directory in the terminal container:

- `muse resume` — interactive session picker for this workspace (pick by name/recency).
- `muse resume --last` — reattach to the most recent session in this workspace.
- `muse resume <session-name>` — reattach by exact Session Name (e.g. `muse resume mild-libration`).
- `muse resume <session-uuid>` — reattach by full session id.

Crashed sessions remain resumable the same way: the log is append-only, so the
pre-crash state is intact. If the picker looks empty, confirm you are in the
right workspace (`--workspace` overrides it).

## Where things live

- Session logs: `/root/.local/share/muse/sessions/YYYY/MM/DD/<session-id>/session.jsonl`
- Subagent logs: same dir under `subagent/<child-id>/session.jsonl`
- Large tool outputs: same dir under `tool-outputs/`

## If resume misbehaves

- `muse resume --last` attaching to the wrong session → use the picker or a name.
- Stale/duplicate entries → reattach by UUID from the log directory listing.
- Truly wedged runtime state → start a fresh session and point it at the old
  session id; never delete logs to "fix" resume.
