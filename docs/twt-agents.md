# Audited local and hosted agents on S5

TwT is installed in `/home/ari/twt/venv` from a pinned BoomTick commit
on `devin/1795061083-scope-allowed-precedence`; the installed package's
`direct_url.json` records the exact revision. The existing
`/home/ari/repos/boomtick` is not used.

The `twt.service` user unit runs a deployed copy of
`deep-agent/twt_service.py`, listening on host `127.0.0.1:8000` and a
private Unix socket, never a wildcard or bridge network interface.
It is enabled under `default.target`; user lingering keeps it running
without a login and across reboots. The unit uses `UMask=0077` and
`NoNewPrivileges=yes`.

The canonical user unit is `config/twt.service`. Deploy the service script
and unit only while no pipeline is active, then reload the user manager
and restart **only** `twt.service`. It creates `~/twt/run` mode 0700 and
`gateway.sock` mode 0600. One process serves both listeners, sharing the
same gateway policy state. Only terminal/buttons mount that directory,
read-only at `/run/twt`; no keys or database directories are mounted.
Their `TWT_BASE_URL=http://twt/v1` and
`TWT_UDS=/run/twt/gateway.sock` select HTTPX Unix-socket transports for
sync/async OpenAI and native Ollama clients, plus receipt lookup/recovery.
Host runs leave `TWT_UDS` unset and keep loopback. Explicit diagnostic
provider clients do not use the TwT socket. There is no TCP fallback when
a configured socket is missing or unavailable.

Compose mount/environment changes require a later container recreate;
do not restart containers running jobs. After they are available, probe
without inference:

```sh
docker compose exec -T terminal curl --fail --unix-socket /run/twt/gateway.sock http://twt/v1/models
```

`/home/ari/twt/environment` is mode 0600, inside a mode-0700 directory.
It sets `TWT_DAILY_BUDGET_USD=5` and
`TWT_DB=/home/ari/twt/catalog.sqlite3`. Provider keys are environment-file
entries; map `GEMINI_API_KEY` from the approved source to `GOOGLE_API_KEY`.
Only available credentials are copied. Missing providers remain disabled.
Additional entries such as `NVIDIA_API_KEY`, `GROQ_API_KEY`,
`CLOUDFLARE_API_TOKEN`, and `CLOUDFLARE_ACCOUNT_ID` are read on restart:

```sh
systemctl --user restart twt.service
systemd-run --user --wait --pipe \
  --property=EnvironmentFile=/home/ari/twt/environment \
  /home/ari/twt/venv/bin/twt sync
/home/ari/twt/venv/bin/twt --db /home/ari/twt/catalog.sqlite3 models
/home/ari/twt/venv/bin/twt --db /home/ari/twt/catalog.sqlite3 report
```

## Routing

`TWT_BASE_URL` defaults to `http://127.0.0.1:8000/v1`. Preferred/reachable
Ollama requests use its sibling `/local/api/chat` endpoint; hosted
requests use TwT's OpenAI-compatible `/v1/chat/completions` default route.
Automatic hosted selection never connects directly to OpenRouter/Nebius.
Explicit diagnostic provider preferences remain available.

The local adapter uses **the pinned TwT Gateway and trace store**, not a
separate audit format. Its provider is named `ollama-local`. Native
Ollama options, JSON format, thinking, images, and tool calls are retained.
`S5_LOCAL_ONLY=1` forces this local-only endpoint, which has no hosted
provider in its configuration. `NEBIUS_ONLY=1` is a legacy force-remote
flag and now selects TwT, not direct Nebius.

The adapter defaults to `S5_TWT_OLLAMA_MODEL=qwen3:8b` and
`S5_TWT_OLLAMA_URL=http://127.0.0.1:11434`. It permits only a loopback
upstream and that exact model; `cloud` names are rejected. Immediately
before each inference, `/api/show` must identify a completion-capable
model with neither `remote_model` nor `remote_host`. This restricts the
route to installed local models without disabling cloud globally or
restarting the shared Ollama daemon. Keep agent `ASK_MODEL` aligned with
the service's configured model.

Hosted provider catalog includes Google, Groq, NVIDIA, OpenRouter,
SambaNova, Cloudflare (with 10,000 daily free Neurons), Mistral, and Nebius.
Free-tier providers precede paid providers; Nebius is the paid fallback.
Local and hosted traces share the same private database
and appear together in `twt inspect` and `twt report`.

The shared model router caps every pipeline stage at 8192 output tokens
and 64000 serialized history characters. Oversized tool results and old
assistant output are compacted before complete old tool exchanges are
dropped; system/user instructions and the latest exchange are preserved.
Oversized instructions fail rather than being silently discarded.
TwT waits up to 60 seconds total per request for free-provider RPM or
Retry-After cooldowns, with at most one retry of a free-provider 429.
Paid fallback follows only after the free routes fail or exceed that bound.
Zero-priced free attempts cost zero even when errors omit token usage.

Hosted ledger costs use TwT's `x-cost-usd` header or `usage.cost`.
Coder trace aggregation preserves the reported cost. Missing, partial,
negative, or non-finite costs remain unknown and fail the spend gate
closed rather than being repriced from the gateway's dynamic model name.
HTTP errors also retain TwT's cost headers in the ledger, independently
of whether completion token usage is available.

Each pipeline request carries a UUID trace ID, durably journaled under
`<state>/receipts/`. On return, timeout, or cancellation the pipeline asks
TwT's loopback-only `/v1/receipts` endpoint to stop undispatched fallbacks
and waits at most 200 seconds for already-dispatched calls to settle.
Reconciliation joins exact IDs against ledger rows, refreshes aggregate
costs, and inserts missing receipts without double billing. Unsettled or
unknown receipts fail closed; a journal can be resumed after a hard kill
with `python3 deep-agent/twt_receipts.py <journal.jsonl>` using the original
ledger/state environment. Receipt responses contain billing metadata, not
prompts. TwT checks client disconnection again after cooldowns and before
upstream dispatch, so a disconnected client cannot start paid fallback.

Coder command execution remains the allowlisted `run_check` tool with its
cwd pinned to the issue worktree. The disk backend deliberately does not
implement an unrestricted sandbox shell; its unavailable `execute` tool
is removed from the tool registry, not merely hidden from the model.

The service adapter tests require installing the pinned TwT package in
the test environment in addition to the deep-agent dependencies.

Coder, spec and audit models keep the default TwT waterfall but send
`provider_models: {"nvidia": "z-ai/glm-5.3-flash", "nebius": "zai-org/GLM-5.3-Flash"}`.
NVIDIA serves the free GLM tier before Nebius; local and other free routes remain intact.
Each stage sends `provider_reasoning_effort: {"nvidia": "low", "nebius": "low"}`.
TwT translates this into `reasoning_effort=low` and GLM chat-template
`reasoning_effort=low, clear_thinking=true` only at those providers.
GLM thinking cannot be disabled; omitted effort defaults to `max`.
See [NVIDIA's model guide](https://docs.nvidia.com/nim/large-language-models/latest/get-started/advanced/get-started-glm-5.3-flash.html)
and [Nebius's API](https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion).
The policy lives in `model_router.STAGE_NVIDIA_MODELS` and `STAGE_NEBIUS_MODELS`; optional
`TWT_CODER_NEBIUS_MODEL`, `TWT_SPEC_NEBIUS_MODEL` and `TWT_AUDIT_NEBIUS_MODEL`
override individual stages, with corresponding `TWT_<STAGE>_NVIDIA_MODEL` overrides.
Local-only models never carry hosted selections or reasoning controls.
TwT validates provider selections, preserves them in traces and strips this
gateway-only field before upstream dispatch. An unavailable selected model
fails closed rather than silently substituting a cheaper model at that step.
