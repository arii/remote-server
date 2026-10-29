# Evaluation, Button Audit, and Next Steps — 2026-10-29

## 1. Executive Summary & Current Stack State

All three repositories are verified on their latest releases, fully synchronized with GitHub, and wired into the automated webhook / hook infrastructure:

| Repository | Current Release / Head | Open PRs | Auto-Pull Webhook | Tracked Post-Merge Hook |
|---|---|---|---|---|
| **`remote-server`** | `c5a0aa0` (latest `main`) | 0 | Registered (`704473987`) | `/srv/remote-server/.git/hooks/post-merge` |
| **`tech-dancer`** | `1fbc9c9` (`v0.10.0`) | **0** (all merged clean) | Registered (`704473991`) | `/srv/repos/tech-dancer/.git/hooks/post-merge` |
| **`boomtick`** | `37fd8da` (`cli-v0.15.1`, `mcp-v0.7.1`) | **0** (clean release) | Registered (`704572541`) | `/srv/repos/boomtick/.git/hooks/post-merge` |

- **Live Buttons Service:** Rebuilt and reloaded as `remote-buttons:local`. Both `boomtick` and `tech-dancer` return live briefings with zero open PRs and clean boards.
- **Auto-Approval Configured:** `/root/.gemini/antigravity-cli/settings.json` permits `git`, `docker`, `gh`, `npm`, `pnpm`, `td`, `python3`, `curl`, `bash`, `sh`, and `/opt/venvs/td/bin/pip` without manual prompt clicking.
- **Valid Access URL:** The phone UI is reached at:
  ```text
  https://dev-onasafari.ddns.net:8443/buttons
  ```
  (Entering plain `buttons` in a browser search bar fails because `buttons` is an internal route on port 8443, not a standalone URL.)

---

## 2. Issues Investigated & Resolved During Reload

### A. Repos Dropdown Multi-Repo Discovery (`b66a676`)
- **Issue:** Dropdown previously only listed `remote-server`.
- **Cause:** `_discover_repos()` ran `git -C <path> config --get remote.origin.url` without `-c safe.directory=<path>`. Git flagged dubious ownership across the container `root` and host `1000` (`ari`) boundary and returned exit 1.
- **Fix:** Added `-c safe.directory={host_path}` to `_discover_repos()`. Dropdown now dynamically lists `boomtick`, `remote-server`, and `tech-dancer`.

### B. BoomTick State Synchronization & Release Upgrade
- **Issue:** `boomtick` was out of sync on server, had an unstaged schema diff, lacked a GitHub webhook, and missed node dependencies.
- **Fixes Applied & Verified:**
  - Fast-forwarded `boomtick` to `37fd8da` (`cli-v0.15.1`, `mcp-v0.7.1`).
  - Ran `pnpm install --frozen-lockfile` (479 packages added for MCP and dev tooling).
  - Reinstalled `boomtick-cli 0.15.1` in editable mode (`pip install -e /srv/repos/boomtick/cli`) inside `/opt/venvs/td`.
  - Registered GitHub webhook `704572541` on `arii/boomtick` and verified incoming push deliveries.
  - Pre-warmed `.agent-context.json` via `td context-warm`.

### C. Tech-Dancer PR Queue Cleared
- **`chore(main): release tech-dancer 0.10.0` (#4814)** has been merged into `main`. Both #4827 and #4826 were incorporated.
- The `tech-dancer` board now reports 0 open PRs and clean statusCheckRollups. Context has been refreshed via `td context-warm`.

---

## 3. Button UX Evaluation: Mobile Pain Points & Proposed Polishing

User testing confirmed that button feedback on mobile needs targeted refinement:

1. **`Sync & Nudge Agents` Graceful Handling:**
   - Currently, if `JULES_API_KEY` is not in `.env`, `td agent list-sessions` exits with code 1 (`ValueError: JULES_API_KEY is not set or empty`), which turns the entire status banner red.
   - **Recommendation:** Wrap step 1 to check for `JULES_API_KEY` or catch exit 1 and report: `"Notice: JULES_API_KEY is unset; add to .env to enable active session syncing."` without turning the run status red.

2. **Error Extraction for Mobile Status Banner:**
   - The current banner (`failed (exit 1) — see "step failed" line at the end of output`) forces users to scroll through hundreds of lines in a mobile `<pre>` tag.
   - **Recommendation:** Extract the top-level error line or JSON `"message"` from the output and display it directly in the `#state` banner.

3. **Preflight JSON Digest:**
   - `td preflight` returns 30KB+ of nested JSON with oxlint/pylint/semgrep reports.
   - **Recommendation:** Render a compact checklist (`Doctor: OK, Typecheck: OK, Lint: Warnings`) instead of dumping raw JSON into the output pre-block.

---

## 4. Current Button Inventory

| Button | Target Repo | Role on Phone UI | Current Status |
|---|---|---|---|
| **Status** | Stack | Quick health check (git status, compose ps, terminal image). | Active & fast (<1s). |
| **Deploy buttons** | Stack | Rebuilds the buttons container via `terminal` container without self-kill. | Active. |
| **Recreate terminal** | Stack | Recreates `terminal` after `.env` changes or if `zellij` hangs. | Active escape hatch. |
| **Logs** | Stack | Tails 100 lines of `terminal` + `ollama` + Pi session audit. | Active. |
| **Repo briefing** | Selected | Fast (~7s) triage of open PRs, CI, conflicts, and reviews. | Active (shows 0 PRs on clean boards). |
| **Sync & nudge agents** | Stack / Repos | Syncs Jules sessions and runs automated CI feedback check. | Active (needs `JULES_API_KEY`). |
| **Warm context** | Selected | Regenerates `.agent-context.json` for agent tools. | Active (~1s). |
| **Preflight check** | Selected | Full-stack doctor, typecheck, lint, and merge simulation. | Active. |
| **Agent suggests next actions** | Selected | Pi analyzes briefing and renders allowlist-validated one-tap action buttons. | Active (`ef294e5`). |

---

## 5. Pi Agent Session Audit & Performance Analysis (`/root/.pi/agent/sessions/`)

An audit of live session logs across `/root/.pi/agent/sessions/--srv-remote-server--/` and `/root/.pi/agent/sessions/--srv-repos-tech-dancer--/` was conducted to diagnose real-world latency, tool failures, and user abort patterns.

### Hardware & Execution Profile
- **Host Hardware:** TRIGKEY S5 running an AMD Ryzen 7 PRO 5850U (8 cores / 16 threads, Radeon APU).
- **Execution Mode:** CPU-only Ollama inference with zero discrete GPU acceleration.
- **Observed Throughput:** ~3.5 to 5.5 tokens/sec for `qwen3:8b`, and ~1.8 tokens/sec for `qwen3:14b`.

### Session Benchmark Telemetry

| Session ID | Workspace / CWD | Model | Prompt / Task | Duration | Outcome & Bottlenecks |
|---|---|---|---|---|---|
| `01a1863a` | `/srv/remote-server` | `qwen3:14b` | "read about boomtick mcp tools..." | **252.8s** (call) + **165.0s** (gen) | **Aborted by user (>7 min total).** 14B model on CPU took >4 minutes just to emit 2 tool calls, then spent 2.75 min generating reasoning before abort. |
| `01a18641` | `/srv/remote-server` | `qwen3:8b` | "Read Agent Contract resource..." | N/A | Aborted / abandoned immediately by user before completion. |
| `01a18644` | `/srv/remote-server` | `qwen3:8b` | "Read Agent Contract resource..." | **88.5s** (call) + **64.7s** (loop) | **Failed & aborted.** Model called `read_mcp_resource` with `server: "workspace"` and `server: "repo"`. Tool errored; model looped in 64s of reasoning before user ran `/clear`. |
| `01a18647` | `/srv/remote-server` | `qwen3:8b` | "init" / "are contexts warm" | **56.5s** + **99.3s** | Delayed clarification (56.5s) followed by a 99.3s call to `list_mcp_resources` because no explicit context warmth tool exists. |
| `01a18634` | `/srv/repos/tech-dancer` | `qwen3:8b` | "Suggest next actions (empty context)" | **107.9s** | **Severe waste on empty state.** Generated 588 tokens over 1.8 minutes just to output `SUGGEST: none`. |
| `01a18639` | `/srv/repos/tech-dancer` | `qwen3:8b` | "Suggest next actions (empty context)" | **93.8s** | 532 tokens over 1.5 minutes to output `SUGGEST: none`. |
| `01a1864a` | `/srv/repos/tech-dancer` | `qwen3:8b` | "Suggest next actions (PR #4833)" | **187.5s (3 min 7s)** | 322 tokens on PR #4833 took over 3 minutes on mobile buttons interface. |

### Core Root Causes Identified

1. **Unsuppressed Reasoning Tokens on CPU:**
   - Qwen3 models emit 300–500 hidden thinking tokens even when `thinkingLevel: "off"` is set. On CPU at ~4 tok/sec, every action incurs a mandatory 60–120s latency penalty before the user receives a single character or before a tool executes.
2. **MCP Resource URI vs. Server Name Mismatch:**
   - `boomtick-mcp` exposes resources like `mcp://workspace/agent-contract` and `repo://package-json`.
   - Pi's client tool `read_mcp_resource` requires two separate arguments: `server` and `uri`.
   - LLMs infer `server: "workspace"` or `server: "repo"` from the URI authority/scheme, yielding:
     `MCP server "workspace" has no resources. Servers with resources: boomtick`
   - This failure triggered unhandled recovery loops that stalled the agent until aborted.
3. **No Direct Context Warmth / Status Tool:**
   - When asked "are contexts warm", the agent had to fall back to `list_mcp_resources` (~99s) because neither `td` nor `boomtick-mcp` provides a lightweight check like `repo.get_context_status` with cache timestamps.
4. **Lack of Fast-Path Triage for Empty States:**
   - "Agent suggests next actions" calls full LLM inference unconditionally. Even when the preflight context clearly shows `Needs attention: Nothing`, the user waits ~100s for the LLM to reiterate that nothing needs doing.
5. **Outdated Pi MCP Configuration:**
   - `/root/.pi/agent/mcp.json` hardcodes `npx -y @arii/boomtick-mcp@0.6.2`, missing the latest `0.7.1` updates and using network-bound npx invocations instead of the local repo.

---

## 6. Tracked GitHub Issues & Implementation Roadmap

To resolve the performance bottlenecks and tool bugs uncovered during session auditing, the following GitHub issues were created and prioritized across the repositories:

### Newly Filed Issues

1. **`arii/boomtick#1012`**: [`fix(mcp): align resource URI schemes with MCP server discovery and add context status check tool`](https://github.com/arii/boomtick/issues/1012)
   - **Scope:**
     - Add explicit server attribution metadata to all MCP resource definitions and document canonical server targeting.
     - Add a first-class `repo_get_context_status` (or `repo.get_context_status`) tool returning cache timestamp, warmth flag, and stale file count.
     - Add dedicated `repo_read_agent_contract` tool to bypass generic `read_mcp_resource` URI parsing ambiguities.
     - Support dynamic workspace/repo paths in MCP tools instead of static single-repo environment bindings.

2. **`arii/tech-dancer#4834`**: [`perf(agent): deterministic fast-path triage for agent suggestions and graceful JULES_API_KEY fallback`](https://github.com/arii/tech-dancer/issues/4834)
   - **Scope:**
     - Implement deterministic rule-based triage in `td agent suggest` / buttons backend: if `open_prs == 0`, `failing_runs == 0`, and `active_sessions == 0`, return `SUGGEST: none :: All queues clear and checks green.` immediately in <50ms without invoking Ollama.
     - Gracefully handle missing `JULES_API_KEY` in `td agent list-sessions` to prevent exit 1 crashes and status banner failure.

### Related Open Issues Tracked
- **`arii/boomtick#1010`**: [`bug(mcp): jules.list_sessions returns raw array causing structuredContent schema validation failure`](https://github.com/arii/boomtick/issues/1010)

### Remote-Server Stack Optimizations Underway
- **Pi Configuration Update:** Update `/root/.pi/agent/mcp.json` to pin `@arii/boomtick-mcp@0.7.1` (or point directly to `/srv/repos/boomtick/mcp/dist/index.js` via node to eliminate npx overhead).
- **Model Tuning:** Benchmark non-reasoning coder models for Ollama CPU (e.g. `qwen2.5-coder:7b-instruct` or `qwen2.5-coder:3b`) to eliminate the 300–500 token thinking overhead and reduce suggestion latency from 180s down to <15s.
