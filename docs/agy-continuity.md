# agy continuity — verdict: survives the Muse cutoff (issue #400)

**Verdict: agy survives.** Its backend and credentials are entirely
separate from Muse's. Probed 2026-11-04 in the terminal container.

## Chain

| Link | agy | muse (for contrast) |
|---|---|---|
| Binary | `/root/.local/bin/agy` v1.4.12 (Antigravity CLI, `local-data` volume) | `/root/.local/bin/muse` (image-baked) |
| Backend | Google Antigravity API — turns hit `https://daily-cloudcode-pa.googleapis.com/v1internal` (seen in `antigravity-cli/log/cli-*.log`) | Meta Muse service |
| Default model | `Gemini 3.8 Flash (Medium)` (`agy models` also lists Gemini 4 Argon, Claude, GPT-OSS — all served via Antigravity, none via Muse) | Muse model |
| Credential | Google OAuth token at `/root/.gemini/antigravity-cli/antigravity-oauth-token` (mode 600), persisted in the `gemini-data` volume | `/root/.config/muse/auth.json`, persisted in the `muse-data` volume |
| API-key env | `GEMINI_API_KEY` is empty/unset — auth is the OAuth token file, not an env key | n/a |

## Independence evidence

- `agy -p "Reply with exactly this word and nothing else: PONG"` →
  `PONG`, exit 0, empty stderr (print-mode turn works).
- Zero references to `muse` in agy settings
  (`/root/.gemini/antigravity-cli/settings.json`),
  `/root/.gemini/config/`, and the turn's CLI log.
- agy settings carry no credential fields at all — only
  `allowNonWorkspaceAccess`, `mcpServers`, `model`, `permissions`,
  `trustedWorkspaces`. Auth lives solely in the OAuth token file.
- The two providers share no files and no volumes: killing
  `muse-data` (the cutoff scenario) leaves `gemini-data` untouched.

## What to watch

- The OAuth token can expire or be revoked independently of any
  cutoff. If agy turns start failing with auth errors, re-authenticate
  the Antigravity CLI in the terminal container; no repo change needed.
- `agy` self-updates into `/root/.local/bin` (`local-data` volume),
  so the binary survives container recreates but is not pinned by the
  image build. If a bad upstream release breaks print mode, pin or roll
  back the binary in the volume.

## Cutoff flip runbook (issue #399)

The agent drawer defaults to `providers[0]` from
`ordered_providers()` (`backend/exec/agent_turn.py`), which reads
`AGENT_DEFAULT_PROVIDER` per request — default `muse`;
`muse`|`agy` (case/whitespace-insensitive, anything else keeps the
muse-first order). Flip muse→agy with env only, no code change:

1. Set `AGENT_DEFAULT_PROVIDER=agy` in `.env` (see `.env.example`).
2. `docker compose up -d --build buttons`
3. Verify `GET /api/agent/providers` lists `agy` first.

## Scope note

`backend/exec/agent_turn.py` runs
`agy --output-format stream-json -p` /
`agy --conversation <cid> --output-format stream-json -p`
(issue #440; `--continue` is never used — it resumes the global
most-recent conversation).
No replacement provider needed.
