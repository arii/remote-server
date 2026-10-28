> ## ⏸️ PARTLY ON HOLD — Pi activated 2026-10-27
>
> Ari activated **Pi + local Ollama** on 2026-10-27 (see
> `docs/pi-setup.md`); the rest of this evaluation (OpenClaw, Copilot
> automation, Gemini route) stays deferred. In `docker-compose.yml` the
> `ollama` service is active and `openclaw` / `actions-runner` remain
> commented out under a `profiles: ["agents"]` block.
>
> ---

# Agent harness evaluation

**Date:** 2026-10-22. **Hardware:** TRIGKEY S5 — Ryzen 7 PRO 5850U (8C/16T Zen 3, no AVX-512), 32 GB DDR4 shared with Vega 8 iGPU, no dGPU, residential NAT. **OS plan:** Ubuntu Server 24.04 LTS + Docker + Ollama (CPU inference). Realistic local envelope: 8B–14B Q4_K_M. **Goal:** (i) 24/7 personal assistant, (ii) automated code review on github.com/arii.

## 1. Resolving "pi" — the ambiguity

Three plausible referents:

- **Pi, the open-source terminal coding agent by Mario Zechner (now Earendil Inc).** Describes itself literally as *"a minimal agent harness"*. Terminal CLI + RPC server + TypeScript SDK, MIT, `pi.dev`, repo `earendil-works/pi`, npm `@earendil-works/pi-coding-agent`. **v1.0.0 shipped 2026-10-01.** ~84–100k GitHub stars. 4 default tools (read/write/edit/bash), ~1k-token system prompt, 15+ providers incl. Ollama, MCP via extensions, print/JSON/RPC modes for scripting.
- **Inflection's Pi** (pi.ai) — a consumer chatbot. Not an agent harness, no local-model story, Inflection pivoted to enterprise. Dismissed.
- **PyPI `pi-coding-agent`** (Ashutosh0428/pi-agent) — a smaller, unrelated Python project that happens to share the name. Shows up in package searches; not the real thing. Not evaluated.

**Evaluated referent: Zechner/Earendil Pi.** Deciding evidence: Ari wrote *"agent harness like pi"*, and Pi's own positioning is exactly "minimal agent harness". It is also the dominant "pi" in the terminal-agent space in 2026 (84k+ stars, Pi 1.0 released three weeks ago).

## 2. Harness evaluations

### 2.0 Summary table

| Harness | Type | Version (Oct 2026) | Local-model config | (i) Assistant fit | (ii) Code-review fit | Cost |
|---|---|---|---|---|---|---|
| Pi (Zechner) | Minimal terminal coding harness (CLI/RPC/SDK) | 1.0.0 (2026-10-01) | `~/.pi/agent/models.json` provider block: `baseUrl` → Ollama `/v1`, `api: "openai-completions"`; `compat` flags for missing roles/params | Medium — scriptable, but no scheduler/chat channels; needs a wrapper | **Strong** — read-only tool flags (`--tools read,grep,find,ls`), JSON/RPC modes for CI | Free, OSS (MIT) |
| LangChain / LangGraph | Agent framework (Python/TS) | LangChain 1.x (core 1.6.6), LangGraph 1.2.12; `create_agent` front door | `ChatOllama` (`langchain-ollama`); `OLLAMA_HOST` / `base_url` | Strong — full control, durable state, custom channels | Strong — build exactly the review loop you want; more code | Free, OSS (MIT); LangSmith optional |
| OpenClaw | 24/7 personal AI agent w/ chat channels (WhatsApp/Telegram/Discord/Slack), skills, cron | v2026.10.x (stable 2026.10.3; 2026.10.4 released Oct 16) | `~/.openclaw/openclaw.json`: `models.providers.ollama`, `baseUrl: http://127.0.0.1:11434` (**no `/v1` suffix**), `api: "ollama"` → native `/api/chat`; `agents.defaults.model.primary: "ollama/<tag>"` | **Strongest** — built for exactly this: always-on gateway, memory in Markdown, scheduling, `/model` switching local↔cloud | Medium — has repo tools, but heavyweight and noisy for per-PR automation | Free, OSS (MIT); you pay model API if cloud |
| OpenHands | Autonomous software-engineering agent (web control center, Docker sandboxes) | v1.28.0 (2026-10-19) | `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` env (LiteLLM); `openai/<model>` prefix for OpenAI-compatible endpoints | Weak — session-oriented, heavy (Docker-in-Docker), not a 24/7 companion | Medium — good autonomy, but assumes frontier-class models; PR-trigger path is DIY | Free, OSS (MIT) |
| Aider | Terminal AI pair programmer (git-aware, repo map, diff edits) | 0.86.2 (Oct 2026) | `--model ollama_chat/<tag>`; `OLLAMA_API_BASE` | Weak — interactive pair programmer, not a daemon | **Strong** — architect/editor modes, repo map keeps context small; ideal engine for a review-diff loop | Free, OSS (Apache-2.0); BYO model |
| Goose (AAIF / ex-Block) | On-machine general agent (CLI/desktop/API), MCP extensions, recipes, scheduler | v1.55.0 (Oct 16, 2026) | `goose configure` → Ollama provider (`~/.config/goose`); built-in llama.cpp offline mode | Strong — scheduler + recipes + chat UIs; closest OSS alternative to OpenClaw for always-on | Medium — can be scripted via CLI/API, but no review-specific scaffolding | Free, OSS (Apache-2.0) |
| Qodo PR-Agent | Purpose-built automated PR reviewer (GH Action / Docker / webhook / CLI) | current 2026 (Qodo donated to community; MIT per 2026 guides) | `config.model: "ollama/<tag>"`, `OLLAMA.API_BASE` (LiteLLM under the hood); self-hosted runner required for localhost | Weak — single-purpose reviewer, not an assistant | **Strongest** — `/review`, `/describe`, `/improve`, inline comments, auto-trigger on PR events | Free self-hosted; managed Qodo Merge from $19/user/mo |

### Detailed notes per harness

**Pi (evaluated referent)**
- What: ~1k-token-system-prompt harness; 4 tools by default (read, write, edit, bash), grep/find/ls available on demand. Sessions as branchable trees. Extensible via TypeScript extensions, skills, prompt templates ("pi packages" via npm/git). OpenClaw docs cite Pi as a real-world integration (Pi can serve as OpenClaw's coding backend).
- Local LLMs: first-class. `~/.pi/agent/models.json` provider with `baseUrl` → Ollama `/v1`, `api: "openai-completions"`, plus a `compat` block (`supportsDeveloperRole: false`, `supportsReasoningEffort: false`) because Ollama chokes on the `developer` role / `reasoning_effort` Pi sends by default. `~/.pi/agent/settings.json` sets `defaultProvider`/`defaultModel`. Switch mid-session with `/model`.
- Small-model quality: the tiny system prompt and 4-tool surface are *the* reason Pi is the best-behaved harness on 8B–14B models — fewer tool schemas in context = fewer malformed calls. Still needs a tool-trained model (see §3).
- Fit: (i) no built-in scheduler, chat apps, or memory — you'd wrap it (cron + a chat bridge). (ii) excellent: `--tools read,grep,find,ls` gives a read-only reviewer; `--mode print` / JSON / RPC makes it CI-scriptable with zero extra scaffolding.
- Cost: free.

**LangChain / LangGraph**
- What: the framework, not a product. LangChain 1.0 (Oct 22, 2025) reset the API: `create_agent` is the front door, LangGraph is the runtime underneath (durable state, checkpoints, interrupts, time-travel). AgentExecutor/LCEL-era patterns deprecated.
- Local LLMs: `langchain-ollama` `ChatOllama` (or `langchain-community`); `OLLAMA_HOST` env or `base_url` param; any OpenAI-compatible endpoint via `langchain-openai` with custom `base_url`. Tool calling works against Ollama's `/api/chat` and `/v1` — but see the OpenClaw `/v1` streaming caveat; for LangChain, prefer non-streamed or tested paths with small models.
- Small-model quality: framework-agnostic — quality is the model's. LangChain adds overhead (prompt bloat from middleware) that hurts small models; keep the agent minimal or use raw SDK calls for the tight loop.
- Fit: (i) best if you want *your* assistant with *your* memory semantics (LangMem/Mem0/Postgres checkpointer), custom tools, and full control. More build work than OpenClaw/Goose. (ii) best if you want a bespoke review pipeline (diff fetch → chunk → review → post comments) with human-in-the-loop approval gates — LangGraph interrupts are made for this.
- Cost: free; LangSmith tracing optional (free tier 5k traces/mo).

**OpenClaw**
- What: the 24/7 personal-agent product: gateway daemon on your box, talk to it from WhatsApp/Telegram/Discord/Slack, skills (incl. ClawHub marketplace), cron jobs, Markdown-file memory, control UI. History: Clawdbot (Nov 2025, Peter Steinberger) → Moltbot (Jan 27, 2026, Anthropic trademark pressure) → OpenClaw (Jan 2026), now under the independent OpenClaw Foundation. ~392k stars, the most-starred agent project on GitHub as of Oct 2026.
- Local LLMs: verified working Oct 13, 2026 (Ollama 0.40.3 + OpenClaw 2026.10.2). Critical gotcha: **do not point `baseUrl` at `/v1`** — Ollama's OpenAI-compat streaming path drops tool-call delta chunks (upstream issue #5769), so the agent *narrates* instead of acting. Use `baseUrl: http://127.0.0.1:11434` with `api: "ollama"` (native `/api/chat`). Known-good local primaries per Oct 2026 guides: `qwen3.8:27b`, `gemma4:26b`, `qwen3-coder:30b`, `glm-4.7-flash`, `gpt-oss:20b`, `gemma4:e4b` — all carry Ollama's `tools` tag. Hybrid routing (`/model ollama/...` vs `/model anthropic/...`) is the documented power pattern.
- Small-model quality on *this box*: the recommended primaries are 20B–30B class — too slow on a 5850U CPU (see §3). A 14B Q4 tool-trained model will work for chat/memory/light tools; expect sub-agent orchestration and browser-relay to degrade below ~30B (documented by OpenClaw guides).
- Fit: (i) strongest out-of-box — it *is* the always-on assistant use case. (ii) usable (it can read repos and run commands) but it's a chat-first creature; PR automation would fight its design.
- Cost: free; security posture matters: CVE-2026-25253 (8.8, one-click RCE via Control UI) and CVE-2026-32922 (9.9) both in 2026, 722 published fixes since Jan, and Snyk found flaws in 36% of sampled ClawHub skills. **Run current release, bind to localhost, reach it via Tailscale — never expose the gateway or Control UI to the open internet. Audit every skill before installing.**
- Could not verify: whether any post-2026.10.4 release changed the native Ollama provider defaults; the 2026.10.5 beta's "worker-local inference" was not evaluated (beta only).

**OpenHands (formerly OpenDevin)**
- What: autonomous SWE agent with a web control center; spins Docker sandboxes per task, browses, edits, runs tests. All-Hands-AI. Very active: v1.28.0 released 2026-10-19.
- Local LLMs: supported via LiteLLM (`LLM_MODEL`/`LLM_BASE_URL`/`LLM_API_KEY`, or Settings → LLM → Advanced). Community runs it against Ollama and OpenAI-compatible proxies.
- Small-model quality: **the weak link.** OpenHands' event/action loop (dozens of tool schemas, long trajectories, browser actions) was designed around frontier models. On 8B–14B local models it is widely reported as fragile: malformed actions, lost task state, runaway loops. Usable locally only with 24B+ tool-strong models (Devstral-class) — outside this box's comfortable envelope. Also resource-heavy (nested Docker) for a mini PC.
- Fit: (i) poor — interactive session tool, not a companion. (ii) moderate — it *can* do repo work autonomously, but the model requirement kills the local story; better suited to cloud-model runs.
- Cost: free.

**Aider**
- What: git-aware terminal pair programmer; builds a repo map (tree-sitter) so the model sees structure, not whole files; every change is a git commit (undoable); architect/editor split for hard tasks. Apache-2.0.
- Local LLMs: `--model ollama_chat/<tag>` (LiteLLM routing), `OLLAMA_API_BASE` env. An Oct 2026 hands-on test ran `aider --model ollama_chat/qwen-coder-3b` fully local: 2/3 simple tasks passed on a 3B model — the workflow works, model quality is the ceiling.
- Small-model quality: Aider is unusually local-friendly because the repo map shrinks context and whole-file/edit-block formats are simple. Qwen2.5-Coder/Qwen3-Coder 7B–14B do simple edits, refactors, test fixes. Multi-file reasoning and "don't touch the tests" instruction-following degrade fast below ~14B.
- Fit: (i) poor — interactive CLI, no daemon/scheduler. (ii) **strong as the review engine**: `aider --message "<review prompt>" --no-auto-commits` over a PR diff is a proven pattern; architect mode lets a cheap cloud planner drive a local editor later.
- Cost: free.

**Goose (extra pick #1 — justified: the strongest always-on *assistant* alternative to OpenClaw)**
- What: Block's on-machine agent (Jan 2025), donated Dec 2025 to the Linux Foundation's Agentic AI Foundation; repo moved Apr 2026 to `aaif-goose/goose`, docs at `goose-docs.ai`. CLI + desktop + API, MCP-native (70+ extensions), recipes (reusable workflows), built-in scheduler, parallel subagents, approve/smart-approve modes. Apache-2.0. ~55k stars.
- Local LLMs: `goose configure` → Ollama provider (host `localhost`), model picker lists pulled tags; also LM Studio, and since Apr 2026 a built-in llama.cpp backend that downloads GGUFs directly. Goose's own 2026 docs demo `qwen3:8b` doing multi-turn tool calls.
- Small-model quality: Goose's own guidance is honest — small models handle file/shell work, hallucinate on longer multi-step tasks; use the largest tool-calling model the box can run and verify outputs. Qwen3 8B/14B are the sweet spot here.
- Fit: (i) strong — scheduler + recipes + desktop app + API = a credible 24/7 assistant with less attack surface and less hype-churn than OpenClaw, at the cost of no chat-app channels (Telegram etc. need manual bridging). (ii) moderate — scriptable via CLI, but no review-specific features.
- Cost: free.

**Qodo PR-Agent (extra pick #2 — justified: the only evaluated option purpose-built for automated PR review, with documented Ollama support)**
- What: open-source engine behind Qodo Merge (Qodo donated it to the community). Posts PR descriptions, structured reviews, inline code suggestions, `/review` `/describe` `/improve` `/ask` commands. Runs as GitHub Action, Docker (`pragent/pr-agent`), webhook server (:3000), or CLI.
- Local LLMs: documented: `config.model: "ollama/qwen2.5-coder:32b"`, `config.fallback_models: '[]'`, `OLLAMA.API_BASE: "http://localhost:11434"` (LiteLLM). Needs a **self-hosted Actions runner** (GitHub-hosted runners can't reach your localhost) — which is exactly what the TRIGKEY box is.
- Small-model quality: PR-Agent's prompts are structured and diff-scoped (not free-form agentic loops), so it degrades more gracefully on small models than OpenHands/Pi-style loops — see §3 on where small models genuinely work (triage, nits, summaries).
- Fit: (i) none — single-purpose. (ii) strongest — auto-review on PR open, posts comments natively to GitHub.
- Cost: free self-hosted; only cost is model inference (zero local, cents per review on cheap cloud).

Not evaluated in depth (honorable mentions): **OpenCode** (terminal harness, ~216k stars, v1.19.3 — same slot as Pi, heavier defaults); **Continue/Cline** (IDE assistants — wrong form factor for a headless server); **crewAI/AutoGen** (multi-agent frameworks — heavier than LangGraph for these two use cases); **Letta** (ex-MemGPT — pivoted Mar 2026 from server-side memory to local-first "Letta Code"; memory-architecture churn makes it a shaky pick right now).

## 3. Key analysis — local models for agentic code review: realistic vs hype

**Blunt summary:** a 5850U with no dGPU can run a genuinely useful *triage-grade* reviewer locally today. It cannot run a *senior-engineer-grade* reviewer locally. The gap is model quality, not harness plumbing — every harness above already accepts an Ollama endpoint.

**Tool-calling reliability at 8B–14B Q4.** Tool calling is a harder skill than chat, and small models are bimodal: with a *tool-trained* model and a *small tool surface* it works; otherwise it doesn't.
- Reliably tool-call at this size (verified Oct 2026 guidance): **Qwen3 / Qwen3.5** (`qwen3:8b`, `qwen3:14b`, `qwen3.5:9b` — Qwen reports 66.1 BFCL-V4 on the 9B), **Qwen2.5-Coder / Qwen3-Coder** (7B–14B), **Ministral 3 8B**, **Gemma 4 E4B** (`gemma4:e4b`, Ollama `tools` tag), **gpt-oss:20b** (MoE, 3.6B active — but slow on CPU, see below), **hermes3**.
- Brittle or broken: Llama 3.1 8B (native tool calling exists but fragile with many tools), anything without explicit function-calling training (phi3:mini et al. ignore tools entirely), and *any* small model handed 20+ tool schemas (this is why Pi's 4-tool default and PR-Agent's structured prompts outperform OpenHands-style loops on the same model).
- Rule of thumb: keep the tool surface ≤ ~8 tools, keep the system prompt short, and verify recovery from one failed tool call before trusting any unattended loop.

**Context windows for real repos.** Ollama tags in 2026 advertise 32K–256K contexts, and Qwen3-coder-class models genuinely have 256K. On this box the binding constraint is **KV-cache RAM and speed**, not the advertised window: a 14B Q4 at 32K context needs several extra GB of RAM and prompt-processing slows linearly. Practical: cap `num_ctx` at 8K–16K for review jobs, feed the model the *diff* plus a repo-map/file excerpts — never whole repos. Aider's repo-map approach and PR-Agent's diff-scoped prompts exist precisely for this.

**Where small local models genuinely work vs fail (code review).**
- Work: PR triage and summarization, style/lint-level nits, missing-test detection, docstring/Changelog drafting, "explain this diff", first-pass bug spotting in small diffs (< ~300 lines), re-reviewing after fixes.
- Fail: cross-file reasoning (the bug is three calls away), subtle concurrency/security flaws, API-misuse that requires knowing the *intended* contract, large refactors, and — critically — *judgment*: small models over-comment (nit spam) and under-detect (miss the real bug). Expect a 5–10% actionable-comment rate after tuning; budget for prompt iteration.
- Hype to discard: "local 8B replaces a senior reviewer", "MoE active-params make big models free on CPU" (bandwidth still counts — gpt-oss:20b measured *slower* than dense 14B on CPU in Sept 2026 testing), "256K context means the whole repo fits" (it fits in the window the way a library fits in a wheelbarrow).

**Token throughput on the 5850U (CPU-only, Ollama/llama.cpp).**
- Measured reference points (CPU-only, Q4, warm): 8B ≈ 5.7 tok/s (Sept 2026, desktop CPU); 8B ≈ 2.8–6.8 tok/s (Aug 2026, cloud VPS CPUs); 8B ≈ 8–15 tok/s (Ryzen 9 7950X); 14B ≈ 3.5 tok/s; 33B ≈ 1.5 tok/s. Generation is memory-bandwidth-bound.
- **Estimate for the 5850U (8C/16T Zen 3, dual-channel DDR4-3200, no AVX-512): ~6–12 tok/s for 8B Q4_K_M, ~3–6 tok/s for 14B Q4_K_M, ~2–4 tok/s for 20B MoE.** *Estimate — not measured; measure on first boot with a timed prompt and record it.*
- Practical consequence: a 500-token review from an 8B model takes ~1–2 minutes; from a 14B, ~2–4 minutes. Fine for background PR review (event-triggered, nobody waiting interactively). Not fine for interactive chat — keep the assistant's interactive model at 8B and warm (`OLLAMA_KEEP_ALIVE=24h`, never `keep_alive=0`).
- The Vega 8 iGPU can technically accelerate via Vulkan in llama.cpp, but it's marginal and fragile in Docker; **the CPU path is the stable choice**. Set the BIOS iGPU reservation to the minimum — carved-out VRAM *reduces* the 32 GB available to models.

## 4. Three model routes — use what Ari already pays for

Ari already holds three inference routes. Split duties across them instead of defaulting to pure-local. Pricing/model facts verified Oct 2026; re-check vendor pages before budgeting.

### Route 1 — Local Ollama on the S5

- Cost: $0 marginal. Always-on, fully private, no network dependency.
- Best for: the 24/7 assistant chat surface, triage ("is this PR worth a deep look?"), summarization, style nits, repo Q&A over small contexts.
- Limits: tool-calling/agentic reliability on 8B–14B Q4 (§3). Keep local agents on short-horizon tasks.

### Route 2 — GitHub Copilot (Pro included, $0 marginal)

Copilot's agentic features are included with Pro within its usage metering (premium requests/credits — check the current Copilot plans page; the metering moves).

- **Copilot code review**: agentic review with full-project context gathering; posts comments/suggestions on PRs. Configurable automatic review: on PR open, on draft→open, on every push, or on drafts. Eligible on Pro. Caveat: Copilot always leaves a *Comment* review — never Approve/Request-changes — so it does not satisfy required-approver rules; treat it as a first-pass reviewer, not a gate.
- **Copilot CLI**: GitHub-aware coding agent in the terminal (`/plan`, diff review before anything runs). Runs on the S5 inside the tmux/ttyd session from the phone — no extra infrastructure.
- **Coding agent**: assign a GitHub issue to Copilot and it branches, edits, and opens a PR in the cloud.
- Best for: **default PR review on github.com/arii** — zero setup, native to GitHub, no self-hosted review pipeline to build or maintain.

### Route 3 — Gemini API on the $10 GCP credit

- **Gemini CLI** (`google-gemini/gemini-cli`, Apache 2.0): open-source terminal agent — file/shell tools, MCP support, `GEMINI.md` project instructions, non-interactive `-p` mode for scripts, and a GitHub Action for PR reviews / issue triage. This is very likely the "agy" CLI Ari mentioned (unconfirmed — the name matches no known tool exactly; `gemini` is the closest fit). Note the June 2026 change: free "Sign in with Google" tier was replaced by Antigravity CLI for unpaid users — but a **paid API key still works**, which is exactly Ari's $10-credit path.
- Pricing (Gemini Developer API, Oct 2026): Flash 3.6/3.7/3.8 at **$0.75/1M input + $3.75/1M output through Dec 31, 2026, doubling to $1.50/$7.50 on Jan 1, 2027**. Cheaper: Gemini 2.5 Flash $0.30/$2.50; 2.5 Flash-Lite $0.10/$0.40. Thinking tokens bill as output.
- Rough quantification: a PR review of ~30K input tokens + ~3K output tokens ≈ $0.034 on 3.8 Flash → **$10 ≈ ~290 such reviews**. On 2.5 Flash ≈ $0.017 each → ~580+. Batch API is 50% off. A free tier exists but quotas are too low for agentic loops — overflow only, not the plan.
- Credit-exhaustion fallback, in order: (1) drop to 2.5 Flash-Lite for bulk triage; (2) shift review load to Copilot code review (Route 2, already paid); (3) shift to local models (Route 1). The $10 is a runway, not a foundation — and the Jan 2027 price doubling halves whatever runway is left. (DeepSeek's API at ~$0.27/$1.10 remains a fallback if Gemini doesn't fit.)

### Recommended split of duties

| Workload | Route | Why |
|---|---|---|
| 24/7 assistant chat, quick questions | 1 — local Ollama | always-on, private, $0 |
| Triage: which PRs/issues need attention | 1 — local Ollama | cheap first pass |
| Default PR review on arii repos | 2 — Copilot code review | native to GitHub, $0 marginal, auto on push |
| Deep agentic review (cross-file, large diffs) | 3 — Gemini CLI/API while credit lasts | stronger reasoning than local 8B; ~290-review runway per $10 |
| Terminal agent work from the phone | 2 — Copilot CLI, or 3 — Gemini CLI | both run in tmux over the ttyd session; no new infra |
| Scheduled batch review / repo-wide audits | 3 — Gemini CLI `-p` / GitHub Action | non-interactive, scriptable; Batch API halves cost |

Fallback chain when the $10 runs out: Gemini free-tier quotas → 2.5 Flash-Lite → Copilot review → local models. Nothing in the plan hard-depends on the credit surviving; it just buys ~300–600 high-quality reviews first.

## 5. Recommendation — phased path

**Phase 1 (now): three routes, split by duty.**
- 24/7 assistant: **OpenClaw** on the box (Docker, localhost-bound, Tailscale for phone access), primary model **`qwen3:14b`** via native Ollama provider (no `/v1`). It is the only option that *is* the product Ari asked for (always-on, chat from phone, memory, cron). Accept its security diligence as ongoing ops (pin versions, audit skills).
- Default automated code review: **Copilot code review** on github.com/arii. Enable automatic review on PR open/push. $0 marginal on Pro, zero self-hosted plumbing. Remember it is comment-only — human approval still gates merges.
- Heavy agentic review: **Gemini CLI / PR-Agent on a self-hosted runner**, pointed at the Gemini API while the $10 credit lasts (see §4 for the runway math). Run via `gemini -p` in a workflow step, or PR-Agent's GitHub Action with `config.model: "gemini/<flash-model>"` via LiteLLM. Local Ollama stays as the fallback model.
- Interactive coding help (zero extra infra): **Pi** in the terminal against the same Ollama — its minimal harness is the best-behaved local agent on 8B–14B, ideal for quick repo questions and small edits. Copilot CLI and Gemini CLI are also one `tmux` pane away.
- *Alternative if Ari wants zero cloud anywhere:* PR-Agent with `config.model: "ollama/qwen2.5-coder:14b"` and a tuned prompt, accepting triage-grade output. Honest, works, free.

**Phase 2 (when models improve): move review local.** The trigger is a ~14–24B tool-strong coder that runs ≥10 tok/s on this CPU with <5% nit-spam — e.g., a future Qwen-coder or Devstral-class dense model in the 10 GB envelope. Then flip the review model to `ollama/<tag>` and drop the cloud keys. Re-evaluate quarterly; the local-models frontier is moving fast (Qwen3.5/3.6/3.8 and Gemma 4 all landed within the last year).

**Phase 3 (optional):** if the assistant outgrows OpenClaw's opinions, reimplement the loop in **LangGraph** (`create_agent`, Postgres checkpointer) reusing the same Ollama backend and prompt assets — the skills and review prompts from Phases 1–2 transfer directly.

**What runs 24/7 on the box vs event-triggered:**
- 24/7 (Docker `unless-stopped`): Ollama (model server), OpenClaw gateway, Tailscale (phone access), the GitHub Actions self-hosted runner (idle until a PR event).
- Event-triggered: Gemini CLI / PR-Agent review jobs (GitHub `pull_request` webhook → runner → review via Gemini/local Ollama → comments posted), OpenClaw cron skills, Pi sessions (on-demand terminal).
- GitHub-side (no box resources): Copilot code review.

**Why not the others:** OpenHands needs frontier-class models and nested Docker — wrong for this box. Aider is excellent but interactive-only; it becomes the *engine* inside a LangGraph custom loop in Phase 3, not the product. Goose is the fallback if OpenClaw's security churn or chat-channel model doesn't fit — same Ollama backend, less built-in. LangChain from day one is over-engineering; buy the product (OpenClaw), build the framework only when the product constrains you.

## 6. Integration notes

Docker-compose stack (service names, images, ports, env var *names*, volumes):

- **`ollama`** — image `ollama/ollama:latest`. Ports: `127.0.0.1:11434:11434` (loopback only; other containers reach it via the compose network as `http://ollama:11434`). Env names: `OLLAMA_KEEP_ALIVE` (set `24h`), `OLLAMA_NUM_PARALLEL` (`1`; raise only if RAM allows — KV cache scales with it), `OLLAMA_HOST` (`0.0.0.0:11434` inside container). Volume: `ollama-data:/root/.ollama` (models + blobs). No GPU reservations (CPU-only box). Bootstrap: `ollama pull qwen3:14b`, `ollama pull qwen2.5-coder:14b`, `ollama pull qwen3:8b`.
  - Sizing: qwen3:14b Q4_K_M ≈ 9–10 GB; qwen2.5-coder:14b ≈ 9 GB; qwen3:8b ≈ 5–6 GB. Only one 14B resident at a time comfortably alongside OpenClaw (~1–2 GB) and the runner; use `OLLAMA_KEEP_ALIVE` per-model discipline or accept load latency (~30–60 s from SSD).
- **`openclaw`** — image: OpenClaw's published container (pin tag, e.g. `openclaw/openclaw:2026.10.4`; verify tag at deploy time). Ports: none published (or `127.0.0.1:<port>` only for the Control UI). Env names: none secret-bearing required for local-only; model routing lives in config file. Volume: `./openclaw-data:/root/.openclaw` holding `openclaw.json` (JSON5) with `models.providers.ollama` = `{ baseUrl: "http://ollama:11434", api: "ollama", apiKey: "ollama-local", models: [{ id: "qwen3:14b", name: "...", contextWindow: 32768 }] }` and `agents.defaults.model.primary: "ollama/qwen3:14b"`. Depends on `ollama`. Restart: `unless-stopped`. Access: Tailscale only.
- **`actions-runner`** — image `ghcr.io/actions/actions-runner:latest` (or the official runner image; pin version). Env names: `REPO_URL`, `RUNNER_TOKEN` (ephemeral registration token — injected at deploy, never committed), `RUNNER_NAME`, `LABELS` (e.g. `self-hosted,linux,local-llm`). Volume: `./runner-data:/home/runner/_work`. Needs network access to `http://ollama:11434` for the local-fallback path. Restart: `unless-stopped`.
- **Review automation** — no dedicated service: runs as steps inside Actions workflows on the self-hosted runner. Two options: (a) `gemini -p "<review prompt>"` via the Gemini CLI with `GEMINI_API_KEY` in GitHub Secrets; (b) PR-Agent Docker action (`pragent/pr-agent`) with `config.model: "gemini/<flash-model>"` (LiteLLM naming — verify against PR-Agent docs at deploy), `config.fallback_models: ["ollama/qwen2.5-coder:14b"]`, `OLLAMA.API_BASE: "http://ollama:11434"`, `github_action_config.auto_review: true`. Trigger: `on: pull_request: types: [opened, synchronize, reopened]`. Copilot code review is configured GitHub-side (repo settings), no workflow needed.
- **`pi`** — no service: npm-install `@earendil-works/pi-coding-agent` on the host (or a small toolbox container) for interactive use; `~/.pi/agent/models.json` provider `{ baseUrl: "http://127.0.0.1:11434/v1", api: "openai-completions", apiKey: "ollama", models: [...], }` with the `compat` block (`supportsDeveloperRole: false`, `supportsReasoningEffort: false`).
- **Networking:** single compose network; only Tailscale exposes anything off-box. Ollama never published to LAN. OpenClaw Control UI never published beyond loopback.
- **Bootstrap order:** `ollama` → pull models → `openclaw` (verify `openclaw doctor --deep`, `openclaw models status`, watch `/api/chat` in Ollama logs) → register `actions-runner` → enable Copilot code review on one repo + open a test PR.

**Could not verify / open questions:** exact tok/s on the 5850U (estimated in §3 — measure on first boot with `ollama run` + a timed prompt and record it); whether OpenClaw's post-2026.10.4 releases change Ollama provider defaults (pin and read the changelog at deploy); PR-Agent's exact license text at the pinned commit (guides say MIT in 2026; historically Apache-2.0 — check the repo's LICENSE file when pinning); Gemini API pricing drift and the Jan 2027 doubling (re-check before spending the credit); Copilot Pro metering details for code review/CLI usage.

---

**One-line recommendation:** OpenClaw 24/7 on local `qwen3:14b` for the assistant and Pi in the terminal for interactive work; Copilot code review (Pro included, $0 marginal) as the default PR reviewer on arii repos; Gemini API/CLI on the $10 credit for heavy agentic review (~300–600 reviews of runway) — then flip review fully local when a 14–24B tool-strong coder runs comfortably on the 5850U.

**Single biggest caveat:** On CPU-only inference a 14B model generates at roughly 3–6 tokens/second, so any unattended agent loop (OpenClaw cron jobs, local PR-Agent reviews) must be designed around multi-minute latencies and small context windows — the harness choice matters less than accepting that the local model is a triage-grade assistant, not a senior engineer, until either the models or the hardware change.
