# Session-Record Contract (Pi-first)

Frozen contract for Pi session browser data. Covers `pi_logs.py`
(`--list-json`, `--session-json`) and the `buttons/server.py` handlers
`GET /buttons/api/pi-sessions`, `GET /buttons/api/pi-session`,
`POST /buttons/api/pi-sessions/delete`, `POST /buttons/api/pi-sessions/clear`.

No new Jules surface. Jules display is out of scope post-Nov-11.

## 1. List response

`GET /buttons/api/pi-sessions` → `{"sessions": [<record>, ...]}`,
newest first by `mtime` descending. Empty set → `{"sessions": []}`.

## 2. Session-record schema (list rows)

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | string | yes | Basename only, ends `.jsonl`. `pi-file`: real transcript basename; `background`: `bg-<rid>.jsonl`; `ledger`: `ledger-<n>.jsonl`. Never contains `/`. |
| `id` | string | no | Present for background (`bg-<rid>`) and ledger (`ledger-<n>`); pi-file rows omit it (identify by `file`). Consumers must accept absent. |
| `mtime` | number | yes | Unix epoch seconds (float or int). pi-file: file mtime; background: `finished_at` or `started_at`; ledger: entry `ts` or 0. |
| `cwd` | string | yes | pi-file: session header `cwd` or `""`; background: `/srv/repos/<repo>`; ledger: `""`. |
| `repo` | string | yes | pi-file: last path segment of `cwd`, else dir-name heuristic, else `""`; background: run `repo`; ledger: `""`. |
| `title` | string | yes | pi-file: first user-prompt text, max 80 chars, else `"Untitled session"`; background: `instruction` (or kind default), truncated to 80 (`77 + …`); ledger: `"<script> · <model> · N tool calls"`, max 80. |
| `kind` | string | no | `ask`/`suggest` for background; `"ledger"` for ledger rows; absent for pi-file rows. |
| `source` | string | no | `"background"` for background rows, `"ledger"` for ledger rows, absent for pi-file rows. Absent means pi-file origin. |
| `done` | boolean | no | Background (`r.done`) and ledger (`true`) rows only. |

Per-source minimal shapes:

- pi-file: `file, mtime, cwd, repo, title`.
- background: above plus `id, kind, source="background", done`.
- ledger: `file, id, mtime, cwd="", repo="", title, kind="ledger", source="ledger", done=true`.

Error shape for detail endpoints: `{"error": string, ...}` (optional `matches`/`output`).

## 3. Session-detail schema

`GET /buttons/api/pi-session?session=<basename>`:

| Field | Type | Required | Notes |
|---|---|---|---|
| `file` | string | yes | Echo of requested basename (pi-file/background/ledger). |
| `id` | string | no | Background `bg-<rid>` / ledger `ledger-<n>`; pi-file detail uses `_parse_session_file` `id` (header id, may be `""`). |
| `cwd` | string | yes | Same origin as list `cwd`. |
| `repo` | string | yes | Same origin as list `repo`. |
| `started` | number\|string | no | pi-file: header `timestamp` string (may be `""`); background: `started_at` epoch; ledger: entry `ts`. Accept any. |
| `source` | string | no | `"background"` / `"ledger"`; absent = pi-file. |
| `turns` | array | yes (or error) | List of turn objects (possibly empty). |
| `total_entries` | integer | no | pi-file only: JSONL line count. |
| `raw_output` | string | no | Rendered/raw transcript text for fallback view. |
| `output` | string | no | Alias of rendered text (background/ledger). |
| `error` | string | no | Present instead of turns on failure (`No ... matching`, `Multiple sessions match`, `bad session query`/`bad ledger query` with 400, missing with 404). |

Turn object:

| Field | Type | Required | Notes |
|---|---|---|---|
| `role` | string | yes | `user` / `assistant` (also tool roles pass through `_parse_turn`). |
| `text` | string | yes | Plain text (may be `""` when turn carries only tool calls/results). |
| `timestamp` | any | no | Entry/message timestamp when present. |
| `tool_calls` | array | no | `[{"name": string, "input"|"args": object}]`; pi-file only. |
| `tool_results` | array | no | `[{"name": string, "content": string}]`; pi-file only. |

Background detail turns: exactly user `instruction` turn + assistant `reply`/`raw_output`/`error`/`"Run in progress…"` turn. Ledger detail turns: single assistant summary turn plus `raw_output` = ledger line.

Validation charset: detail query must match `[A-Za-z0-9._-]+`; delete body `session` must match `[A-Za-z0-9._-]{1,120}\.jsonl`. Violations → `{"error": "bad session query"|"bad session name"|"bad ledger query"}` (400).

## 4. Delete / clear

- `POST /buttons/api/pi-sessions/delete` `{"session": "<basename>.jsonl"}` → `{"ok": true, "output": string}` or `{"error": string}` (400 bad name, 404 unknown background run).
- `POST /buttons/api/pi-sessions/clear` → `{"ok": true, "output": string}` or `{"error": string}` (500). Clears transcripts + in-memory background runs; never fails on empty set.
- `pi_logs.py --delete/--clear` are text-output CLI modes (exit 0 always); only the HTTP wrappers above are JSON.

## 5. Example record (background list row)

```json
{"file": "bg-1234567890ab.jsonl", "id": "bg-1234567890ab", "mtime": 1720000000.0, "cwd": "/srv/repos/remote-server", "repo": "remote-server", "title": "How does server work?", "kind": "ask", "source": "background", "done": true}
```
