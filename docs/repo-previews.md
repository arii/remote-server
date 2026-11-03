# Repo preview subpaths (`/<name>`)

Repeatable pattern for serving a repo's web UI as a subpath of the S5
ingress: `https://<host>:8443/<name>`, behind the existing single
oauth2-proxy gate (TLS + GitHub OAuth). First instance: `/hrm`.

## The shape (Caddy)

One block per preview in `caddy/Caddyfile`, placed with the other
subpath handles (above the `handle` fallback). No `strip_prefix`:
unlike `/buttons` and `/code`, the app serves its own base path, so
`/<name>/*` passes through untouched (pages, `/<name>/api/*`,
`/<name>/_next/*`, `/<name>/ws`).

```caddy
handle /<name>* {
	reverse_proxy <upstream>
}
```

`<upstream>` is `terminal:<port>` for tier 1, `<service>:<port>` for
tier 2 (below). `reverse_proxy` already handles websocket upgrades.

## App contract

The app MUST, driven by env (root serving stays the local-dev
default):

1. Serve everything under the base path: pages, API routes, WS,
   assets, OAuth callbacks, and internal redirects.
2. Build URLs at runtime from one base-path helper — never hardcode
   `/<name>` or root-absolute paths. (hrm: `getBasePath()` in
   `utils/urls.ts`, fed by `NEXT_PUBLIC_BASE_PATH`.)
3. Handle the bare path: a browser on `/<name>` (no trailing slash)
   must land in the app, not resolve relative links against `/`.
   Next.js `basePath` does this; other stacks need an explicit
   `/<name>` → `/<name>/` redirect (see the `/code` trap in the
   Caddyfile).
4. Keep no internal routes that collide with a sibling preview name.

## Hosting tiers

**Tier 1 — dev server in the terminal container (fast, ephemeral).**
Run the app's dev server manually inside `terminal` on a fixed,
documented port; Caddy targets `terminal:<port>`. Dies with the
container; fine for iterating. This is `/hrm` today (`:3000`).

**Tier 2 — dedicated compose service (robust).** Checklist:

- Service in `docker-compose.yml` on the compose network (no
  published ports; Caddy is the only entry).
- App configured with its base path via env at build/run.
- Caddy block targeting `<service>:<port>`.
- Healthcheck if the stack has a cheap one.

## Checklist: adding `/<name>`

1. App: implement the contract above (base path via env).
2. Pick tier 1 (port in terminal container) or tier 2 (compose
   service per checklist).
3. Caddyfile: add the one-block handle.
4. Verify: bare `/<name>`, a deep page, one API call, WS connect,
   OAuth round-trip if the app has one.
5. Record the preview (name, tier, upstream, port) in the table below.

## Live previews

| Path  | Repo | Tier | Upstream        | Notes                    |
| ----- | ---- | ---- | --------------- | ------------------------ |
| /hrm  | hrm  | 1    | terminal:3000   | Manual `next dev`; §6    |

## `/hrm` conformance

`/hrm` follows the Caddy shape and the app contract (Next.js
`basePath` from `NEXT_PUBLIC_BASE_PATH`, runtime URLs via
`getBasePath()`). Documented deviation: it is tier 1 — the dev
server is started by hand inside the terminal container, so the
preview dies on container recreate and has no compose entry.
Rationale: iteration speed while the dashboard is under active
dev; promotion to tier 2 is the checklist above whenever
robustness matters more. Port 3000 is hrm's until it moves.

## Dry-run: hypothetical `/portfolio`

Adding a portfolio preview touches only the documented spots:

1. Portfolio app reads its base path from env (contract §3).
2. Tier 1: run its dev server in `terminal` on :3001 (or tier 2:
   compose service + env).
3. One Caddy block: `handle /portfolio* { reverse_proxy
   terminal:3001 }`.
4. Verify per checklist §5; add a row to the table.

No new mechanisms, no edits outside the Caddyfile (+ compose file
for tier 2) and the app's own base-path config.
