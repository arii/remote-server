# Deep-agent observability: how to see what is happening

Four layers, cheapest first:

1. `python3 deep-agent/runs.py` — which runs exist.
2. `python3 deep-agent/trace_view.py <run>` — what one run did
   (phone text on stdout, or `--html` timeline).
3. `python3 deep-agent/ledger.py [n]` — run outcomes + token totals.
4. LangSmith (§7) — cloud traces, when a key is configured.

`runs.py` and `trace_view.py` are stdlib-only: they run under
system `python3` with no venv. The writer side (`observe.py`
recorder + middleware) is what produces the files they read.

Protocol shapes live in
[API_CONTRACT_PROPOSAL.md](API_CONTRACT_PROPOSAL.md) (§9); this
doc shows how to read them, not a second copy of the contract.

## 1. Trace event shapes

One JSON object per line in
`deep-agent/traces/<script>_<thread>_<ts>.jsonl`. Every event
carries `ts` (epoch seconds). Per the contract:

| type | fields |
|---|---|
| `run_start` | `script`, `thread`, `model` |
| `model_request` | `n_messages` |
| `model_response` | `usage: {input_tokens, output_tokens, total_tokens}` |
| `tool_call` | `name`, `args_preview`, `duration_s`, `ok`, plus `result_preview` (ok) or `error` (fail) |
| `run_end` | `duration_s`, `ok` |

Previews are capped at 2000 chars by the writer. Raw peek:

```sh
tail -5 deep-agent/traces/ask_investigator_ask-remote-server_*.jsonl
```

## 2. runs.py reader

Stable run ids are `run-<ledger-line>` (the ledger is
append-only). `runs.py` lists ledger runs (newest first)
and resolves a ref — run id, file path, file name, stem
prefix, or thread — to a trace path:

```sh
python3 deep-agent/runs.py            # last 10 runs, compact
python3 deep-agent/runs.py 3          # last 3 runs
python3 deep-agent/runs.py run-0      # run id -> path
python3 deep-agent/runs.py ask-remote-server   # thread -> path
python3 deep-agent/runs.py deep_chat_abc123    # stem prefix -> path
```

Output of the list form:

```text
run-0 script=ask_investigator.py thread=ask-remote-server model=ollama:qwen3:8b ok=True dur=0.15s tools=3
```

Library API (import `runs`, stdlib-only):
`list_runs(limit, ledger_path)` for the id-keyed listing,
`get_run(run_id, ledger_path)` for one entry plus its
trace events, `list_traces(traces_dir, limit)` for raw
trace files (runs not yet in the ledger), and
`resolve_run(ref, traces_dir, ledger_path)` for the
ref-to-path mapping above.

Resolution order: `run-<n>` id, existing path,
`traces/<ref>[.jsonl]`, newest stem/prefix/thread match,
then newest ledger `extra.trace` for that thread.
Unknown refs print
`Sorry, no run found for '<ref>'.` on stdout and exit 1.

## 3. trace_view usage

```sh
python3 deep-agent/trace_view.py <tracefile|run-id> [--html out.html] [--text]
```

- No flags (or `--text`): plain-text summary on stdout, one
  line per event, every line capped at 72 chars with a `…`
  truncation marker — readable on a phone terminal.
- `--html out.html`: single-file self-contained timeline
  (inline CSS, zero JS, no external refs) + a
  `wrote out.html (N bytes, M events)` line on stdout.
- Both flags combine. Malformed lines never raise: they are
  skipped and counted (`skipped N malformed line(s)`).
- Unknown event types render as `? <type>` so new writer
  shapes stay visible instead of breaking old readers.

Real transcript (generated from an actual `traces/` file
written by `observe.TraceRecorder`, rendered by thread):

```sh
$ python3 deep-agent/trace_view.py ask-remote-server --text
```

```text
trace ask_investigator_ask-remote-server_20261102T215159 (11 events)
+0.0s START ask_investigator.py thread=ask-remote-server model=ollama:q…
+0.0s LLM req n_msgs=3
+0.1s LLM resp in=1840 out=96 tot=1936
+0.1s ok grep_repo 0.42s {"pattern": "trace", "paths… -> observe.py: Tr…
+0.1s ok read_repo_file 0.03s {"path": "deep-agent/observ… -> """Run-tr…
+0.1s LLM req n_msgs=5
+0.1s LLM resp in=2310 out=410 tot=2720
+0.1s ERR pr_state 0.8s err=td: Not Found
+0.1s LLM req n_msgs=7
+0.2s LLM resp in=2650 out=388 tot=3038
+0.2s END ok=True dur=0.151s
tokens in=6800 out=894 tot=7694 | tools ok=2 err=1 | dur=0.151s
```

The HTML timeline for the same run shows the same 11 events
as color-coded rows (blue model, green ok tool, red error,
grey boundaries), summary chips (duration, tokens, tool
counts), and collapsible per-tool args/result detail. It is
~5 KB for this run, phone-width capped (`max-width: 44rem`,
viewport meta), and honors `prefers-color-scheme`.

## 4. SSE trace event mapping (for the API owner)

Per the contract (§9), the backend streams each JSONL line
live as an SSE `trace` event on the run's stream and serves
the file with run detail. Framing matches the server's
existing `_send_sse_event` helper (`event: <name>`,
`data: <json>`, blank line):

```http
event: trace
data: {"ts": 1762000001.0, "type": "tool_call", "name": "grep_repo", "args_preview": "...", "duration_s": 0.42, "ok": true, "result_preview": "..."}

event: trace
data: {"ts": 1762000002.5, "type": "run_end", "duration_s": 2.0, "ok": true}
```

Notes:

- One SSE event per trace line, in file order. `data` is the
  raw line bytes (already JSON) — no re-wrapping.
- `run_start` is the first line; clients key the run off its
  `thread` + the ledger row's `extra.trace` path (see §5).
- Malformed lines are skipped by readers; the streamer may
  forward or drop them, but must not break the stream.

## 5. Embedding the HTML in the /api/agents detail view

The `--html` output is designed for embedding: no JS, no
external requests, works in an `<iframe srcdoc>` or as a
served file. Suggested wiring (peer's call):

- Detail endpoint serves the trace file bytes plus a
  `timeline_url` (or inline `timeline_html`) generated by
  running `python3 deep-agent/trace_view.py <run-id>`
  server-side — stdlib-only, so no venv needed on the host.
- The view renders a "Timeline" disclosure containing
  `<iframe srcdoc="<escaped html>" sandbox>`
  (`sandbox` without `allow-scripts`: there is no JS to run).
  Fallback for tiny screens: link to the same HTML as a
  full-page view.
- Live tailing stays SSE (§4); the static timeline is the
  at-a-glance summary once `run_end` arrives.

## 6. Troubleshooting

- `Sorry, no trace found for '<ref>'.` (exit 1): no file,
  run_id, thread, or ledger entry matches. Check
  `python3 deep-agent/runs.py` for exact ids.
- `skipped N malformed line(s)`: the trace has corrupt
  lines (crash mid-write, partial flush). The rest still
  renders; inspect raw lines with `sed -n` on the file.
- Empty `runs.py` (`no runs`): `deep-agent/ledger.jsonl`
  has no rows yet — run any ledgered script first. For
  raw trace files see `list_traces` (§2).

## 7. LangSmith (opt-in cloud traces)

`deep-agent/tracing.py` attaches one `LangChainTracer` to the
`deep_chat.py` stream configs when — and only
when — `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are both
set (`LANGSMITH_PROJECT` optional; these are the current env
names, older ones are dead). Anything else returns no callbacks,
so tracing can never break a run. No key is provisioned for this
project; to enable, fill the placeholders in `.env.example`
(live key in untracked `.env` only, never committed) and rebuild
the venv (`langsmith` is in `deep-agent/requirements.txt`).
Caveat: on ingest failure the LangSmith client echoes the key to
stderr (upstream behavior) — keep container logs trusted, same as
other credential-bearing output.
