# Buttons Local UX Preview Toolkit

Visually preview and test the buttons page locally before pushing changes.
Captures screenshots across all tabs at mobile (phone) and desktop widths in a clean, reproducible 2-minute workflow with zero dependencies on real live stacks or external repositories.

---

## 5-Step Workflow

### 1. Install dependencies
In the toolkit folder, install pinned Playwright:
```bash
cd buttons/ux && pnpm install
```

### 2. Start the local preview server
Launch the preview server (binds `127.0.0.1:18099` by default):
```bash
# From repository root:
./buttons/ux/serve.sh
# Or from buttons/ux:
pnpm run serve
```
*Leave this running in its own terminal or run in the background.*

### 3. Capture all screenshots
Run the snapshot script:
```bash
# From repository root:
node buttons/ux/snap.mjs
# Or from buttons/ux:
pnpm run snap
```
This launches Playwright (headless with `--no-sandbox`), navigates through each tab (`⚡ Actions`, `📋 Briefing`, `🤖 Pi agent`, `📊 Project`, `🧰 Jobs`, `📜 Ledger`), and captures snapshots at both:
- **Phone:** 390x844
- **Desktop:** 1440x900

Screenshots are saved into `buttons/ux/shots/`.

### 4. Inspect the generated screenshots
Confirm layout, card styling, and tab responsiveness in `buttons/ux/shots/`:
- `phone-actions.png`, `desktop-actions.png`
- `phone-briefing.png`, `desktop-briefing.png`
- `phone-pi.png`, `desktop-pi.png`
- `phone-project.png`, `desktop-project.png`
- `phone-jobs.png`, `desktop-jobs.png`
- `phone-ledger.png`, `desktop-ledger.png`

### 5. Stop the preview server safely
When finished, shut down the server safely:
- Press `Ctrl+C` if running in the foreground.
- Or kill by port:
  ```bash
  kill $(lsof -t -i :18099) 2>/dev/null || true
  ```

---

## Critical Guardrails & Safety Warnings

### ⚠️ The `pkill` Trap
**NEVER run `pkill -f python` or `pkill -f server`!**
In many CI, agent (such as Jules), and remote shell environments, `pkill -f python` matches your own shell runner or orchestrator subagent processes and will immediately kill your active session. Always stop the preview server using `Ctrl+C` or `kill $(lsof -t -i :<port>)`.

### 🛡️ Loopback Only
`serve.sh` passes `BUTTONS_HOST="127.0.0.1"`, binding the server strictly to loopback so local development is never exposed on the network.

### 🛡️ Clean Working Tree (Gitignored Scratch Data)
- Screenshots land in `buttons/ux/shots/` which is `.gitignore`d.
- The throwaway preview ledger lands in `buttons/ux/scratch-ledger.json` (also `.gitignore`d), ensuring the real server ledger is never touched.
- Preview repositories and fixture briefings live under gitignored scratch paths (`buttons/ux/.scratch*`). Running the preview never leaves dirty files in `git status`.

### 🛡️ Graceful Degradation & Realistic Fixtures
`seed.py` seeds a realistic briefing fixture (`fixture-briefing.json`) along with simulated ledger records. Even without Docker or terminal containers present, the Actions and Briefing tabs render realistic, populated PR cards, CI summaries, and review items instead of blank or broken error states.
