# BoomTick issue drafts (worktree agent dispatch)

Copy-paste ready. The connector cannot write `arii/BoomTick`, so
Ari's agent files these. Single-user greenfield: no backcompat
shims in any acceptance list. Related open: #1086
(jules_create_session token), #1084 (bare-array
structuredContent) — both needed for the transition period.

---

## Draft 1 — Worktree session lifecycle + real execution

Title: `feat(mcp): worktree lifecycle tools + real execution in SessionManager.dispatch`

Body:

The worktree-agent-dispatch spec is designed but unbuilt:
`SessionManager.dispatch` is bookkeeping only — no process is
ever spawned.

Build:

1. `worktree_create` / `worktree_list` / `worktree_remove` MCP
   tools. Precedent: `createWorktree` in `lib/git.js`. Jailed
   cwd per session, removal refuses dirty trees without an
   explicit flag.
2. Real execution in `SessionManager.dispatch`: shell out to the
   agent CLI inside the session worktree with the composed
   system prompt (draft 2), stream the transcript to
   `agent_logs`, advance checkpoints, and detect done/failed
   from process exit + transcript markers.
3. `agent_status` / `agent_logs` read the live session state.

Acceptance:

- `agent_dispatch` spawns a real CLI process jailed to its
  worktree; `agent_logs` streams the transcript while it runs.
- Checkpoints advance (started → implementing → testing →
  done/failed) without manual moves.
- A failing test suite in the worktree surfaces the session as
  failed with the log tail attached.

---

## Draft 2 — Executor prompt + dispatch composition + model routing

Title: `feat(mcp): executor prompt file, dispatch composition, session model routing`

Body:

Sessions need the executor contract and a model policy.

Build:

1. Add `mcp/src/agents/executor-agent.prompt.md`, adapted from
   remote-server `deep-agent/EXECUTOR_PROMPT.md` (post-approval
   scope, verification discipline, no self-review, report-back).
2. Compose the dispatch system prompt as: executor prompt +
   repair-agent rules + the target worktree's agent contract.
3. Resolve the session `model` param. Mirror the env contract
   both sides already use: `ASK_MODEL`, `OLLAMA_URL`,
   `NEBIUS_API_KEY` from the stack `.env` — never in specs or
   logs. Ladder: local Ollama first, API-token model only for
   multi-step write work or long context.

Acceptance:

- Dispatch with no `model` resolves Ollama per env; with
  `NEBIUS_API_KEY` set, significant work may route to cloud.
- The spawned CLI receives all three prompt layers; a prompt
  rendering test pins the composition order.
- No secret appears in specs, transcripts, or logs (grep test).

---

## Draft 3 — Session skill access + subagent mechanism

Title: `feat(mcp): session skill access (skill_list/skill_read) + subagent mechanism decision`

Body:

Sessions cannot read SKILL.md files today, and the executor
prompt's swarm patterns assume subagents exist.

Build:

1. `skill_list` / `skill_read` MCP tools (or a skills dir
   mounted into the worktree — either satisfies the need),
   exposing the worktree repo's `.agents/skills/`. Repos
   without skills get seeded with the four custom playbooks
   (working-set check, remote deploy safety, service contract
   edits, writing pass — to be written as SKILL.md files).
2. Decide the subagent mechanism BEFORE the executor prompt
   ships: (a) the executing CLI spawns its own subagents, or
   (b) an MCP `spawn_subagent` tool scoped to the session.
   Recommendation: (a) CLI-spawned — zero new MCP surface,
   matches worktree isolation; session transcripts capture the
   delegation. Record the decision in the spec either way.

Acceptance:

- A session can list and read the repo's skills through the
  chosen mechanism (test with a fixture skills dir).
- The swarm-pattern section of the executor prompt names the
  chosen mechanism — no dangling reference.
- Skill content never leaks across sessions (scoping test).

---

## Draft 4 — Rename agent_merge_approved + td parity for local sessions

Title: `feat(cli): submit-pr naming + local session commands (td agent run, skill, session)`

Body:

Two naming/parity problems block the Jules shutdown (Nov 11):

1. Rename or re-scope `agent_merge_approved`: the standing
   principle is submit-PR with no merge authority, and the
   tool name still says merge. Either rename to
   `agent_submit_pr` (preferred, greenfield) or re-scope its
   contract so the name stops lying.
2. `td agent dispatch` currently maps to Jules session
   creation. Jules goes read-only in days — add a local mode:
   `td agent run --worktree … --brief …` hitting the same
   dispatch path as draft 1 without Jules.
3. `td skill list` / `td skill read <name>` so CLI-driven
   sessions can pull playbooks without MCP.
4. `td session status` / `td session logs` mirroring the MCP
   tools.

Acceptance:

- No `merge` authority in any agent tool name or contract.
- `td agent run` dispatches a local worktree session end to
  end (no Jules API involved — test with the key unset).
- `td skill` and `td session` round-trip against the same
  state the MCP tools read.
