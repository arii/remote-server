# Operator comments — 2026-11-06 session (tracked under revision control)

Source: verbatim operator messages from the 2026-11-06 working session
(recovered after a session crash). Each item maps to its tracking
issue/PR and current status. This file is the coordination index: no
minimal one-off issues/PRs — work is batched into the coordinated
items below so CI isn't burned on trivia.

## 1. Co-browsing crashes + fix proposal
Operator reported co-browse crashes with a detailed two-layer diagnosis
(Docker `/dev/shm` exhaustion, zombie renderers, Crashpad/GPU failures;
CDP race between `browser_cdp.py` and `cdp_client.ts`) and concrete
remedies (ack-first `screencastFrameAck`, latest-wins buffer, human
override mutex, atomic `insertText`, `targetCrashed` recovery).
Status: DONE — verified against the tree (5 real defects found) and
shipped as [#1008](https://github.com/arii/remote-server/pull/1008)
(ack-first screencast, latest-wins backpressure, crash recovery, viewer
`dsf` fix, `init: true`, `--disable-dev-shm-usage`) plus
[#1009](https://github.com/arii/remote-server/pull/1009) (Nemotron rate
card unchoking Nebius fallback). Both merged. Live-verified: 6/6
preflight PASS, real-Chrome click round-trip 132 ms.

## 2. Read updated_guide.md and work through it; push PRs, merge on green CI (minus impact analysis); test on tech-dancer/hrm open PRs via the buttons page with MCP; keep going until complete
Status: IN PROGRESS — guide triaged claim-by-claim (see coordinated
issues below). Hourly reassessment loop scheduled (cron `f8b89cbe`,
hourly at :17, auto-expires in 7 days). Open PRs listed on
tech-dancer (#4852, #4847, Jules #4858–#4861) and hrm (#9765, #9825–#9829);
hrm#9826 chat summary attempted (agent path alive, CPU-bound — see 6).

## 3. Full guide items; update all MCP tools/configs; allow Nebius fallback; verify Ollama; verify buttons site live; usability audit (still text-heavy, awful)
Status: IN PROGRESS — Nebius fallback unchoked and verified live
(`resolve_llm(prefer=nebius)` returns a spec); Ollama verified
(qwen3:8b direct generate ~3 s, all models present). boomtick-side MCP
items are boxed (checkout git corrupt + active lane work) and tracked
in the coordinated boomtick issue. Buttons verification + usability
audit: in progress via BrowserMCP.

## 4. All-night allowance; local-first, Nebius only when the task is too large
Policy recorded: Auto stays local (Ollama); explicit per-turn Cloud
picker forces Nebius for large tasks. No `NEBIUS_ONLY=1` global switch.

## 5. Track every skill/MCP/TD tool-call failure; dispatch issues to fix them
Standing rule, owned by the hourly loop. Failure log so far:
- Nebius fail-closed on unrated Nemotron row → fixed by #1009.
- `td rag` missing in buttons image (boomtick-cli 0.19.1 vs 0.19.2)
  → open, in coordinated remote-server issue.
- deep-chat agentic loop >280 s without tokens on CPU Ollama (direct
  generate is fast) → open, in coordinated remote-server issue.
- `/srv/repos/boomtick` git objects corrupt → open, blocks boomtick work.
- False alarm (own tooling): preflight `td_impact` ENV_MISSING was
  caused by invoking with `TD_BIN=false`; real td supports
  `--preview-url`, full preflight is 6/6 PASS. No issue filed.

## 6. Agent RAG + persistent memory must work; docs must let another user replicate
Status: IN PROGRESS — ledger write/read verified live; `search_td`
works where td has `rag` (blocked in buttons image — same item as
above); legacy Chroma index reports `failed` (superseded by the
`td rag` forward path). Replication docs land with this file.

## 7. Hourly loop: reassess, merge, stay aligned to guides, create issues, assess deep agent/Ollama
DONE (loop armed, cron `f8b89cbe`) + ongoing.

## 8. Dismiss/Defer on attention cards has no effect — file an issue, have the agent solve it
Tracked as [#1010](https://github.com/arii/remote-server/issues/1010)
(already open from session recovery). Agent fix dispatched on branch
`fix/1010-attention-actions`. No live cards present at audit time
(`/api/attention` returns `[]` for remote-server/tech-dancer/hrm), so
the fix carries regression tests; live re-verify when cards appear.

## 9. Track comments under revision control
This file.

## 10. Guide-document actions must also be issues
Covered by [#1010](https://github.com/arii/remote-server/issues/1010)
+ the two coordinated rollup issues:
[remote-server#1011](https://github.com/arii/remote-server/issues/1011)
(console/RAG/perf/docs/box) and
[boomtick#1139](https://github.com/arii/boomtick/issues/1139)
(tool-surface/CDP/cleanup).

## 11. No minimal issues/PRs — coordinate, CI is not free
Enforced: 3 issues total (#1010 + 2 rollups); fixes batched per issue
into single PRs; docs batched into one PR with this file.
