# Personal-agent web-client browser plan

Goal: personal agents drive highly-capable web AI clients (Claude,
ChatGPT, Mistral) without needing interactive browser UX, reusing the
proven `gemini-repo-ask` skill shape.

## Locked decisions (user, 2026-11-20)

- Auth: Ariel signs in once per client; agents reuse persisted sessions.
- Data: only public content and submission-destined drafts may be pasted
  into third-party web clients. Nothing private leaves our systems.
- Order: Claude, then ChatGPT, then Mistral (still tentative).

## Substrate: paired first, Grid CDP fallback

- Primary: BrowserMCP paired laptop Chrome, driven from the CLI tools
  on this machine (copilot, agy, muse). Same topology as today.
- Fallback: Chrome CDP on the docker host (headed Chromium Grid at
  `http://browser:4444`, `BROWSER_ENDPOINT`) when BrowserMCP is down.
  Grid rules apply: one session slot (`SE_NODE_MAX_SESSIONS=1`), always
  delete the session afterwards, never leak it.
- Never borrow a tab another driver owns; fresh `tab list` every pass
  (IDs rotate). Never open extra tabs; reuse the client's tab.

## Skill shape (one per client, mirrors `gemini-repo-ask`)

New skills `claude-ask`, `chatgpt-ask`, (tentatively) `mistral-ask`:

1. `tab list`, then select the existing client tab or open the client URL.
2. Pick the cheap/fast model first; escalate only when the task outgrows it.
3. Reuse one chat while context is in sync; new chat only when stale.
4. Structured prompts: state the repo/artifact, demand exact Markdown
   sections, file paths, conciseness. Quote what the client actually
   returned; never invent results.
5. Keep shots/uploads in a workspace bind-mount dir, never `/tmp`.

## Auth (human once, agents never)

- One-time human sign-in per client, in the paired browser profile and
  (for fallback) the Grid agent profile (`~/.s5-shared-browser/agent`).
- Agents never type passwords, MFA, or CAPTCHA responses, and no
  credential ever appears in skill JSON (per `docs/skill-contract.md`).
- If a session expires, the skill stops and reports `auth-expired`;
  re-auth is a human step, not an agent retry loop.

## Data and safety rules

- Allowed paste classes: public repo content, public profiles, drafts
  already destined for outside submission. Everything else stays local.
- Never paste: credentials, tokens, private docs, Civ Robotics material,
  unverified resume claims (team-lead reports, paper finalist status).
- Prompts to an AI client are not "message sends" to people, so they
  are agent-drivable; anything addressed to a real person still follows
  the approval rules (Gmail drafts / `[REVIEW]` docs, Ariel sends).
- Respect the human-wheel gate: `PAUSED_BY_USER` blocks driving.
- Log each ask to the Agent Ledger via `scripts/ledger_post.py` so web
  answers join the same audit trail as everything else.

## Rollout

1. `claude-ask` skill + one verified round (login check, ask, quote).
2. `chatgpt-ask`, same bar.
3. Decide Mistral (Le Chat web vs API) before building `mistral-ask`.
4. Grid-fallback drill: one round per client through CDP to prove the
   fallback path before it is needed.
