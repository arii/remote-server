# S5 Deploy and Verification Checklist

This document details the deployment, configuration fixes, and end-to-end verification steps for the S5 server environment.

---

## 1. Deploy

On S5 (`/srv/remote-server`):

1. Pull the latest commits from `main`:
   ```bash
   git pull origin main
   ```
2. Rebuild and restart the buttons service container:
   ```bash
   docker compose up -d --build buttons
   ```
3. Hard refresh the `/buttons` page in your browser (`Ctrl+Shift+R` or mobile clear cache).
4. Verify the web UI components:
   - **Status banner & indicators**: displays correct connection / service statuses.
   - **Drawer**: slide-out menu opens and closes smoothly.
   - **5 Tabs**: navigation switches cleanly across the 5 primary tabs (Status, Logs, Review, Terminal, Jobs).
   - **Jobs tab**: displays background job queues and execution logs.
   - **Ask Pi**: query input / modal sends requests to Pi agent.
   - **Create plan button**: generates or triggers plan creation without client-side error.

---

## 2. S5-Side Fixes

### A. BoomTick Preflight Pylint Fix
In `/srv/repos/boomtick`, install `pylint` inside the virtual environment:
```bash
cd /srv/repos/boomtick
./setup-agent.sh
# or activate the venv and install directly:
# source venv/bin/activate && pip install pylint
```

### B. Re-run Context Warm
Warm the agent context after the `project_config.json` fix:
```bash
cd /srv/remote-server
td context-warm
```

### C. Verify `scripts/pull-models.sh` Executable Bit
Ensure git retains executable mode `100755` for `scripts/pull-models.sh`:
```bash
git ls-files --stage scripts/pull-models.sh
# Expected mode: 100755
chmod +x scripts/pull-models.sh
```

---

## 3. Terminal & Container Verification

Inside the running `terminal` container:
```bash
docker compose exec terminal bash
```

Run the following checks:
1. **BoomTick Doctor**:
   ```bash
   td doctor
   ```
   Confirm all local diagnostics and health checks pass.
2. **Pi MCP Server List**:
   ```bash
   pi mcp list
   ```
   Ensure BoomTick and configured MCP servers appear active and connected.
3. **Pi BoomTick Call**:
   Test a live BoomTick call through Pi:
   ```bash
   pi "Run a boomtick status check"
   ```
4. **Antigravity CLI (`agy`) on PATH**:
   Verify `agy` is available in PATH:
   ```bash
   which agy
   agy --version
   ```

---

## 4. End-to-End Verification

1. **Code Discipline & Workaround Chain Check**:
   - Verify that no workaround chains exist in PR diffs or shell scripts (`cmd || slightly-different cmd`, try/except ladders dodging root cause failures, or fallback ladders).
   - In code reviews, enforce that any workaround chain is treated as a **review-blocking finding, not a nit**.
   - Demand a single canonical command path addressing the root failure. Fallbacks are only allowed if documented with reasons for platform variance.
2. **Phone-Browser Path**:
   - Connect phone to Tailscale network.
   - Navigate to `https://<tailscale-ip-or-host>/buttons` and `https://<tailscale-ip-or-host>/terminal` via oauth2-proxy authentication.
   - Confirm Google OAuth login succeeds and redirects to the web app.
   - Test terminal takeover button (`/buttons/api/terminal/takeover`) from phone browser.
3. **CI Status**:
   - Check GitHub Actions workflow status on repository `main` branch to ensure all automated checks are green.
