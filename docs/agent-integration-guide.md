# Agent integration guide: peer SPA ↔ deep-agent lane

Peer side: `origin/main` @ `466c6d6`.
Agent side: `origin/arii/muse-set-up` @ `ec63d35`.
Line refs below are to those commits.
New contract only — no Pi/legacy endorsement.

Agent contract source of truth:
`deep-agent/API_CONTRACT_PROPOSAL.md`
(§5 process protocol, §9 trace shapes).

## 1. What the peer has today

### Backend routers (`backend/api/routers/`, registered in `backend/server.py:9-20`)

- `agents.py:39-56` — `GET /api/agents/health`,
  `GET /api/agents/sessions`, `GET /api/agents/sessions/{name}`.
  Backed by `backend/agents/sessions.py`.
  Pi-shaped: lists Pi transcript `*.jsonl` files only.
  No agent runs surface.
- `chat.py:304-316` — `GET`/`POST /api/chat/stream` (SSE),
  `chat.py:319` abort, `chat.py:331` ask, `chat.py:339` suggest,
  `chat.py:347` pi-progress.
  The stream generator (`_chat_events`, `chat.py:183`)
  already spawns an agent process
  (`ask_investigator.py`, `chat.py:204-206`)
  and reads stdout in 16-byte chunks (`chat.py:231`).
  Transport is agent-ready; only the argv changes.
- `chat.py:87-97` — `run_pi_pipeline` raises
  `"pi pipeline not configured"`.
  Pi-shaped stub: ask/suggest have no executor
  in the FastAPI backend (no docker-exec transport).
- `jobs.py` — list/run/clear/run-td/run-plan
  over `backend/exec/engine.py` `RunRegistry`.
  Agent-shaped, but not wired to agent runs.
- `ops.py:219` — `GET /api/workflow-health`.
  Contains a raw-gh mirror (`ops.py:68-70`,
  `gh api .../actions/runs/.../jobs`) — see gap 8.
- `attention/briefing/issues/pulls/repo/system/notify` —
  unaffected by agent wiring.

### Frontend (`frontend/src/`)

- `views/ChatView.tsx:41-58` — SSE pump handles
  `token`/`error` only; ignores `phase`/`done`,
  has no `approval` handling, no resume POST,
  no trace rendering.
- `views/JobsView.tsx` — dispatches allowlisted actions
  (`:13-24`), polls pi-progress for 12-hex ids (`:146`).
  No run-detail view.
- `views/ReviewView.tsx` — PR/issue review + merge/assign.
  Fine as-is; gets cheaper via `pr-view` (gap 8).
- Missing: approval card, resume buttons,
  runs list/detail, trace view.

### `buttons/server.py` (legacy surface, being replaced)

- Chat: `_handle_chat_stream` `:7309`,
  routes `:7553`/`:8365`, abort `:8375`.
- Ask already runs `ask_investigator.py` (`:521-544`),
  parses `FINAL:` + `_parse_proposal`.
- Suggest is Pi print-mode (`:557-574`). Pi-shaped.
- Raw-gh sites: `_pr_view` `:7197-7203`,
  workflow jobs `:8037`, pr-details `:8156-8159`.

## 2. Wiring recipe

Paths: venv python at
`deep-agent/.venv/bin/python`.
Repo argv is `owner/repo`
(`s5tools`: `arii/remote-server` → `/srv/remote-server`,
else `/srv/repos/<name>`).
`thread_id` = chat `session_id`.

### Step 1 — Spawn `deep_chat.py` per turn

Replace the `ask_investigator.py` argv
(`backend/api/routers/chat.py:204-206`,
`buttons/server.py:7351-7353`):

```python
b64 = base64.b64encode(prompt.encode()).decode()
argv = [".venv/bin/python", "deep_chat.py",
        info["gh"], session_id, b64]
```

Stdout is pure answer text — the existing
16-byte chunk reader works unchanged.
Accumulate the full text; the last line may be:

```text
INTERRUPT: {"thread_id": "...", "interrupts": [...]}
```

Exit codes (`deep_chat.py:main`):

- `0` — turn complete, render `done`.
- `1` — failure; stdout already holds a
  `Sorry, ...` line. Stream it, then `done`.
- `2` — bad argv. Server bug: log it,
  emit `error`.
- `3` — needs approval. Parse the
  `INTERRUPT` line, emit `approval` (step 2).
  Keep the SSE connection open.

### Step 2 — `INTERRUPT` line → `approval` event

On exit `3`:

```python
blob = json.loads(interrupt_line[len("INTERRUPT: "):])
value = blob["interrupts"][0]["value"]  # server never sends back ids
emit("approval", {"session_id": sid, **value})
```

Before rendering Approve, pre-validate each
`action_request` (stdlib-only, system python,
no venv needed):

```sh
python3 deep-agent/deep_chat.py --validate-proposal <b64-json>
# stdout: {"ok": true, "text": "PROPOSE td\nCOMMAND: ...\nEND PROPOSE"}
```

If `ok` is false: do NOT render Approve.
Auto-reject (`"message": "invalid proposal"`,
step 3) and emit `phase`/`error` instead.

`ChatView.tsx` sketch — extend the pump:

```tsx
if (ev.name === "approval") showApproval(JSON.parse(ev.data));
// card: kind td -> <code>args.command</code>
//       kind issue -> title + collapsed body
// buttons POST /api/chat/resume; disable after first tap
// (double-tap -> second POST gets 409; swallow it)
```

### Step 3 — `POST /api/chat/resume` + `--resume` flow

```http
POST /api/chat/resume
{"session_id": "abc123", "decision": "approve"}
{"session_id": "abc123", "decision": "reject", "message": "not now"}
```

`202`: `{"ok": true, "resumed": true, "session_id": "..."}`.
Errors: `400` bad shape, `404` unknown session,
`409` no pending approval / turn in progress.

Server spawns (repo recorded for the session):

```sh
.venv/bin/python deep_chat.py owner/repo <thread> --resume approve|reject [b64msg]
```

Same stdout/exit contract as a turn —
exit `3` again means a second `approval` event.
`reject` without a message defaults to
`"Declined by reviewer."`

Serialize: one live turn per `session_id`
(second spawn → `409 turn in progress`).
Cross-thread concurrency is fine.

### Step 4 — Serve `runs.py` for run detail

`deep-agent/runs.py` is stdlib-only — import it
directly (no venv, no subprocess):

```python
sys.path.insert(0, "deep-agent")
import runs

@app.get("/api/agents/runs")
def agent_runs(limit: int = 50):
    return {"runs": runs.list_runs(limit)}

@app.get("/api/agents/runs/{run_id}")
def agent_run(run_id: str):
    row = runs.get_run(run_id)
    if row is None:
        raise HTTPException(404, "unknown run")
    return row
```

Ids are `run-<ledger-line>` (stable, append-only).
`get_run` returns the ledger entry plus its
trace events; corrupt lines are skipped, never fatal.
This is the new agent surface; the Pi
`/api/agents/sessions` listing stays until
the peer retires it (open question 4).

### Step 5 — Stream trace JSONL as SSE `trace` events

Every run writes
`deep-agent/traces/<script>_<thread>_<ts>.jsonl`
(`observe.py:40-50`).
Tail it live; each line is one event:

```python
for line in follow(trace_path):  # ledger extra.trace points here
    emit("trace", json.loads(line))
```

Shapes (`API_CONTRACT_PROPOSAL.md` §9,
`observe.py:12-17`): every event carries `ts`.

- `run_start {script, thread, model}`
- `model_request {n_messages}`
- `model_response {usage: {input_tokens, output_tokens, total_tokens}}`
- `tool_call {name, args_preview, duration_s, ok, result_preview|error}`
- `run_end {duration_s, ok}`

Previews capped at 2000 chars.
The recorder never raises — a missing
trace file means "no observability",
never a failed run.

### Step 6 — Ask flow via `ask_investigator` (PROPOSE protocol)

Keep the existing spawn
(`buttons/server.py:526-527`):

```sh
.venv/bin/python ask_investigator.py owner/repo <b64-instruction>
```

Parse stdout: progress lines, then `REPORT`
(authoritative tool-result lines, 160-char cap),
then `FINAL: <reply>`.
A write the user asked for comes back as:

```text
PROPOSE issue
TITLE: <one line>
BODY:
<markdown>
END PROPOSE
```

or `PROPOSE td` / `COMMAND:` / `END PROPOSE`.
Reuse `deep_chat.py`'s `validate_proposal`
for the td shape check (step 2).
Execution stays server-side behind the
validated run-td / create-issue endpoints —
the agent never executes writes.

Backend gap: implement `run_pi_pipeline`
(`backend/api/routers/chat.py:87`)
by spawning `ask_investigator.py` directly
instead of raising. Needs the venv path
(open question 5).

## 3. Gap list (dependency order)

1. Swap chat spawn to `deep_chat.py` turn
   (`chat.py:204-206`, `buttons/server.py:7351-7353`).
   Unblocks everything below.
2. `INTERRUPT` parse + `approval` event +
   `--validate-proposal` gate (step 2).
3. `POST /api/chat/resume` + per-session
   serialization (step 3).
4. ChatView approval card + resume buttons;
   extend the SSE pump past token/error.
5. `runs.py` list/get endpoints + run detail
   in the UI (step 4).
6. Live `trace` SSE + `extra.trace` link (step 5).
7. Backend `run_pi_pipeline` via `ask_investigator`;
   validate PROPOSE blocks (step 6).
8. After the terminal image rebuild, switch
   three raw-`gh` sites in `buttons/server.py`
   to boomtick cli-v0.18.0 commands:
   - `:7200`
     `gh pr view N --json statusCheckRollup,mergeStateStatus`
     → `td --json gh pr-view --pr N --repo R`.
     Keys: `prNumber, title, state, draft,`
     `reviewDecision, mergeable, checks[], checksSummary`.
     Rewrite the `_ci_label` input mapping and
     the merge-gate strings in `_pr_view` (`:7197`).
   - `:8037`
     `gh api repos/.../actions/runs/.../jobs`
     → `td --json gh run-jobs --run ID --repo R`.
     Same for the backend mirror
     (`backend/api/routers/ops.py:68-70`).
     Check the `run-jobs` payload covers failed
     steps before dropping the annotations call (`:8050`).
   - `:8157` `gh pr view` (pr-details)
     → `pr-view`. Map files/stats/author/draft
     from the new keys.
   (The deep-agent lane's own `pr_state` switch
   in `ask_investigator.py:118-143` is handled
   separately — not the peer's job.)
   Also grep while there: `:7863` (list view),
   `:7229` (update-branch PUT).
9. `chat.db` under `deep-agent/.chat_threads/`
   + `langgraph-checkpoint-sqlite` need the
   image lane (out of scope here, §10);
   decide retention (open question 2).

## 4. Open questions for the peer

1. Hold-and-continue vs close-and-reopen the
   SSE stream on approval — does the current
   handler support a second spawn per connection?
2. `chat.db` retention: delete thread state on
   session clear, TTL, or keep forever?
3. Approval cards: reuse the jobs Approve/Decline
   styling, or stay chat-local?
4. `/api/agents/sessions`: retire the Pi listing
   once `/runs` lands, or keep both?
5. Where does the FastAPI backend spawn venv
   python from — what container path?
