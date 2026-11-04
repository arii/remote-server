# Agent integration guide: peer SPA ↔ deep-agent lane

Peer side: `origin/main` @ `466c6d6`.
Agent side: `origin/arii/muse-set-up` @ `ec63d35`.
Code refs below are symbol anchors (file + function/route
name), never line numbers — they survive line shifts.
`backend/tests/test_doc_anchors.py` fails when a cited
backend/ or deep-agent/ symbol disappears.
New contract only — no Pi/legacy endorsement.

Agent contract source of truth:
`deep-agent/API_CONTRACT_PROPOSAL.md`
(§5 process protocol, §9 trace shapes).

## 1. What the peer has today

### Backend routers (`backend/api/routers/`, registered in `backend/server.py` via `include_router`)

- `agents.py` (`health`, `agent_runs`, `agent_run`) — `GET /api/agents/health`,
  `GET /api/agents/runs`, `GET /api/agents/runs/{run_id}`.
  Backed by `deep-agent/runs.py` (`runs.list_runs`, `runs.get_run`).
  Agent-shaped: the runs surface from step 4.
- `chat.py` (`chat_stream_get`, `chat_stream_post`) — `GET`/`POST /api/chat/stream` (SSE),
  `chat_abort` abort, `pi_progress` pi-progress.
  (`/api/ask` + `/api/suggest` were removed in #548.)
  The stream generator (`_chat_events`)
  already spawns an agent process
  (`deep_chat.py` — gap 1 done; `ask_investigator.py`
  is no longer spawned in production)
  and reads stdout in 16-byte chunks (`_chat_events`).
  Transport is agent-ready; only the argv changes.
- `run_pi_pipeline` (removed in #548 with its ask/suggest endpoints) —
  formerly raised `"pi pipeline not configured"`.
  Pi-shaped stub: ask/suggest have no executor
  in the FastAPI backend (no docker-exec transport).
- `jobs.py` — list/run/clear/run-td/run-plan
  over `backend/exec/engine.py` `RunRegistry`.
  Agent-shaped, but not wired to agent runs.
- `ops.py` (`workflow_health`) — `GET /api/workflow-health`.
  Contains a raw-gh mirror (`fetch_workflow_health`,
  `gh api .../actions/runs/.../jobs`) — see gap 8.
- `attention/briefing/issues/pulls/repo/system/notify` —
  unaffected by agent wiring.

### Frontend (`frontend/src/`)

- `views/ChatView.tsx` (`pumpStream`) — SSE pump handles
  `token`/`error` only; ignores `phase`/`done`,
  has no `approval` handling, no resume POST,
  no trace rendering.
- `views/JobsView.tsx` — dispatches td commands (`onDispatchTd`),
  polls pi-progress for 12-hex ids (`fetchPiProgress` in `apiClient.ts`).
  No run-detail view.
- `views/ReviewView.tsx` — PR/issue review + merge/assign.
  Fine as-is; gets cheaper via `pr-view` (gap 8).
- Missing: approval card, resume buttons,
  runs list/detail, trace view.

### `buttons/server.py` (legacy surface — deleted in `1464844`)

Symbols below are for archaeology
(`git show 1464844^:buttons/server.py`); the live
surface is the backend routers + frontend above.

- Chat: `_handle_chat_stream`, stream routes, abort.
- Ask ran `ask_investigator.py`,
  parsed `FINAL:` + `_parse_proposal`.
- Suggest was Pi print-mode. Pi-shaped.
- Raw-gh sites: `_pr_view`,
  workflow jobs, pr-details.

## 2. Wiring recipe

Paths: venv python at
`deep-agent/.venv/bin/python`.
Repo argv is `owner/repo`
(`s5tools`: `arii/remote-server` → `/srv/remote-server`,
else `/srv/repos/<name>`).
`thread_id` = chat `session_id`.

### Step 1 — Spawn `deep_chat.py` per turn

Replace the `ask_investigator.py` argv
(chat.py `_chat_events`; the `buttons/server.py`
spawn is gone with the file — deleted in `1464844`):

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
- `3` (`EXIT_NEEDS_APPROVAL`) — needs approval.
  Parse the `INTERRUPT` line, emit `approval` (step 2).
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
(`observe.py` `default_trace_path`).
Tail it live; each line is one event:

```python
for line in follow(trace_path):  # ledger extra.trace points here
    emit("trace", json.loads(line))
```

Shapes (`API_CONTRACT_PROPOSAL.md` §9,
`observe.py` `TraceRecorder`): every event carries `ts`.

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

Keep the `ask_investigator.py` spawn shape
(`buttons/server.py` deleted in `1464844`; no production
spawn site remains — only the manual `deep-agent/eval_ask.py`
gate drives it):

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

Backend gap: the `run_pi_pipeline` stub is gone
(removed in #548 with its endpoints); re-adding an
ask flow means spawning `ask_investigator.py` directly.
Needs the venv path (open question 5).

## 3. Gap list (dependency order)

1. Swap chat spawn to `deep_chat.py` turn
   (chat.py `_chat_events`; `buttons/server.py`
   deleted in `1464844`).
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
   (deleted in `1464844` — live equivalent is the
   backend `fetch_workflow_health` mirror)
   to boomtick cli-v0.18.0 commands:
   - `_pr_view`
     `gh pr view N --json statusCheckRollup,mergeStateStatus`
     → `td --json gh pr-view --pr N --repo R`.
     Keys: `prNumber, title, state, draft,`
     `reviewDecision, mergeable, checks[], checksSummary`.
     Rewrite the `_ci_label` input mapping and
     the merge-gate strings in `_pr_view`.
   - the workflow-jobs call
     `gh api repos/.../actions/runs/.../jobs`
     → `td --json gh run-jobs --run ID --repo R`.
     Same for the backend mirror
     (`backend/api/routers/ops.py` `fetch_workflow_health`).
     Check the `run-jobs` payload covers failed
     steps before dropping the annotations call.
   - pr-details `gh pr view`
     → `pr-view`. Map files/stats/author/draft
     from the new keys.
   (The deep-agent lane's own `pr_state` switch
   in `ask_investigator.py` `pr_state` is handled
   separately — not the peer's job.)
   Also grep while there: the list view,
   the update-branch PUT.
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
