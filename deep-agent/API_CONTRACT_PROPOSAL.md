# Deep chat agent — API contract proposal

Owner: deep-agent lane (this doc). `buttons/server.py`, `frontend/**`,
and `docker-compose.yml` stay with the peer session — this doc only
proposes the shapes so both sides can meet in the middle.

Status: spike. `deep-agent/deep_chat.py` implements the agent side;
no server wiring exists yet.

> Note: the HTTP paths below (`/buttons/api/...`) are illustrative and
> predate the FastAPI rewrite — the peer session defines the new
> `/api/agents*` contract. The agent process protocol (§5: argv shapes,
> stdout token stream, `INTERRUPT` line, exit codes, approve/reject
> resume) is backend-agnostic and is the stable part to build against.

## 1. Surfaces that do NOT change

- `GET /buttons/api/chat/stream?session_id=...&repo=...&prompt=...`
  stays the turn entry point. `session_id` doubles as the agent
  `thread_id` (charset `^[A-Za-z0-9_-]{1,64}$`, already enforced).
- `POST /buttons/api/chat/abort` semantics unchanged.
- Existing SSE events unchanged: `phase` (`prefill` / `tool_call` /
  `generating` / `done` / `aborted` / `error`), `token` (`{token}`),
  `done` (`{text, aborted, elapsed}`), `error` (`{error}`).
- The Ask flow (`POST /buttons/api/ask`, PROPOSE text protocol) is
  untouched. `propose_action` output reuses the exact PROPOSE block
  shape the server already parses, so the peer can share parsers.
  Ask `REPORT` outcome lines are capped at 160 chars (ellipsis marks
  cuts) for phone-sized progress tails; `FINAL:`/`PROPOSE` shapes are
  unchanged.

## 2. New endpoint: resume a paused turn

The agent pauses mid-turn when the model calls `propose_action`
(HITL `interrupt_on`). The pause survives process exit (sqlite
checkpointer), so resume is a separate request.

```http
POST /buttons/api/chat/resume
Content-Type: application/json

{"session_id": "abc123", "decision": "approve"}
{"session_id": "abc123", "decision": "reject", "message": "not now"}
```

Response `202`:

```json
{"ok": true, "resumed": true, "session_id": "abc123"}
```

Error responses (all JSON `{error: ...}`):

| Condition | Status | Body |
|---|---|---|
| bad `session_id` / `decision` shape | 400 | `{"error": "bad ..."}` |
| unknown `session_id` (no thread) | 404 | `{"error": "unknown session"}` |
| thread has no pending approval | 409 | `{"error": "no pending approval"}` |
| thread already has a live turn running | 409 | `{"error": "turn in progress"}` |

Server behavior on resume: spawn
`deep_chat.py <repo> <session_id> --resume <decision> [b64-message]`
with the repo recorded for that session, and stream its stdout back
over the still-open SSE connection as `token` events (same as a
turn), ending with `done` — or with a second `approval` event if the
model proposes again.

## 3. New SSE event: approval

Emitted on the open `/buttons/api/chat/stream` connection when the
turn process exits `3` (see §5). The stream MUST stay open: `token`
events continue after the user taps Approve/Reject.

```http
event: approval
data: {"session_id": "abc123", "action_requests": [{"name": "propose_action", "args": {"kind": "td", "command": "td --json preflight"}, "description": "Review the proposed write action before it runs."}], "review_configs": [{"action_name": "propose_action", "allowed_decisions": ["approve", "reject"]}]}
```

- `action_requests[]`: one entry per gated call this turn (model
  order). `args` is the model's raw proposal — see §4.
- `review_configs[]`: `{action_name, allowed_decisions}`; the client
  renders one button per allowed decision (`approve` → Approve,
  `reject` → Decline). Unknown decision types are hidden.
- Frontend: tapping a button `POST`s §2 with the same `session_id`.
  Disable both buttons after the first tap (double-tap → second POST
  gets `409`, which the client swallows).

## 4. Validation rules (both sides enforce)

1. The agent re-validates inside `propose_action` post-approve
   (`validate_proposal` in `deep_chat.py`): `kind` is `issue` or
   `td`; issue needs title 1–120 chars + body 1–4000 chars; td
   commands must match an allowlisted shape verbatim except `N` →
   digits (`[1-9][0-9]*`). Anything else returns `REJECTED: ...`
   to the model, which then answers without the proposal.
2. The server MUST pre-validate every `action_request` before
   rendering Approve, via the stdlib-only helper (no venv needed):

   ```sh
   python3 deep-agent/deep_chat.py --validate-proposal <b64-json>
   # stdout: {"ok": true, "text": "PROPOSE td\nCOMMAND: ...\nEND PROPOSE"}
   ```

   If `ok` is false, the server MUST NOT render Approve: auto-POST
   the equivalent of `reject` (`"message": "invalid proposal"`) and
   emit a `phase` `error` event instead of `approval`.
3. Execution stays server-side behind the existing validated
   endpoints (jobs approve flow). The agent never executes writes.

## 5. Agent process protocol (deep-agent lane owns)

Turn:

```sh
.venv/bin/python deep_chat.py owner/repo <thread-id> <b64-prompt>
```

- stdout: pure answer text streamed as token chunks (byte-compatible
  with the current chunk reader).
- A HITL pause appends one final stdout line and exits `3`:

  ```text
  INTERRUPT: {"thread_id": "abc123", "interrupts": [{"id": "...", "value": {"action_requests": [...], "review_configs": [...]}}]}
  ```

  The server takes `interrupts[0].value` and re-emits it as the §3
  `approval` event (adding `session_id`). Interrupt `id`s are
  LangGraph-internal; the server never sends them back.
- exit `0` turn complete; `1` model/runtime failure (stdout holds a
  `Sorry, ...` line — every failure branch prints it to stdout, never
  stderr, with the exception detail capped at ~160 chars and the
  `Sorry, ...` prefix kept verbatim so eval gates still classify the
  run as invalid); `2` bad argv; `3` needs approval.
- Replies are written for a phone-sized screen: the agent prompts ask
  for short paragraphs or a short list, no wide tables.

Resume:

```sh
.venv/bin/python deep_chat.py owner/repo <thread-id> --resume approve|reject [b64-message]
```

Same stdout/exit contract as a turn. `reject` without a message
defaults to `"Declined by reviewer."`. Resume with no pending
approval exits `1` (`no pending approval for thread ...` on stderr).

Decision mapping (agent-internal, shown for audit):

```json
"approve" -> {"type": "approve"}
"reject"  -> {"type": "reject", "message": "..."}
```

resumed as `Command(resume={"decisions": [...]})`, one decision per
`action_request` by position (batched HITL mode).

## 6. State, threads, concurrency

- Threads live in `deep-agent/.chat_threads/chat.db` (sqlite,
  LangGraph checkpointer; git-ignored). One row family per
  `thread_id` = chat `session_id`. History accumulates per thread;
  the server starts a fresh `session_id` for a fresh conversation.
- One live turn per thread: the server MUST serialize turn/resume
  spawns per `session_id` (second spawn → `409 turn in progress`).
  Cross-thread concurrency is fine.
- Retention/rotation of `chat.db` is undecided (proposal: server
  deletes the thread row family when a session is explicitly
  cleared; see open questions).

## 7. Frontend sketch (peer's call)

On `approval`: render the proposal card under the streaming box —
for `kind: td` a `<code>` block of `args.command`, for `kind: issue`
title + collapsed body — plus Approve/Decline buttons wired to §2.
On the following `done`, render the final markdown as today.

## 8. Open questions for the peer

1. Hold-and-continue (recommended, §2) vs close-and-reopen the SSE
   stream on approval — does the current handler support a second
   spawn per connection?
2. `chat.db` retention: delete thread state on session clear, TTL,
   or keep forever?
3. Should `approval` cards reuse the jobs Approve/Decline styling,
   or stay chat-local?

## 9. Trace events (deep-agent lane owns the shapes)

Every run writes `deep-agent/traces/<script>_<thread>_<ts>.jsonl`
(`observe.py`, via documented middleware hooks; never raises). The
backend streams each line live as an SSE `trace` event and serves the
file with run detail. Shapes:

- `run_start {script, thread, model}`
- `model_request {n_messages}`
- `model_response {usage: {input_tokens, output_tokens, total_tokens}}`
- `tool_call {name, args_preview, duration_s, ok,
  result_preview|error}` (previews capped at 2000 chars)
- `run_end {duration_s, ok}`

Every event carries `ts` (epoch seconds). The ledger row's
`extra.trace` points at the file. This is what makes tool calls and
decisions visible; sync on presentation later.

## 10. Deliberately out of scope

- No `backend/**`, `frontend/**`, `buttons/server.py`, or
  `docker-compose.yml` changes in this slice.
- No container rebuild / deploy wiring (`.chat_threads/` and the new
  `langgraph-checkpoint-sqlite` requirement need the image lane).
- No skills, subagents, or Store-backed cross-thread memory — the
  spike is one agent, one thread, five tools.
