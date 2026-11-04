# Workstation Configuration & Multi-Agent Onboarding Guide

This guide details how to configure a new machine from scratch to run the engineering portfolio, Antigravity multi-agent toolset, Boomtick MCP, and Browser MCP (v2.3.0).

---

## 1. System Prerequisites & Base Tooling

Ensure your machine (Linux/Debian/Ubuntu or macOS) has the core system packages and runtimes installed:

### A. Core Package Managers & Utilities

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y git curl wget jq python3 python3-venv python3-pip build-essential

# macOS (Homebrew)
brew install git curl wget jq python3
```

### B. Node.js & pnpm

Use Node 24.x (matching workspace engines) and pnpm 10.28.2:

```bash
# Recommended: via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc # or ~/.zshrc
nvm install 24.16.0
nvm use 24.16.0
nvm alias default 24.16.0

# Install pnpm pinned version
npm install -g pnpm@10.28.2
```

### C. Docker

Docker is required for isolated deployment impact analyses, visual regression diffs, and Playwright container runs:

```bash
# Add current user to docker group to run without sudo
sudo usermod -aG docker "$USER"
newgrp docker
```

### D. GitHub CLI (gh) & Authentication

Authenticate with GitHub to allow PR retrieval and merge conflict analysis:

```bash
gh auth login
```

---

## 2. Repository Layout & Initial Bootstrap

Clone the main repositories side-by-side in your home directory:

```bash
cd ~
git clone https://github.com/arii/portfolio.git ~/portfolio
git clone https://github.com/arii/boomtick.git ~/boomtick
```

### A. Bootstrap portfolio

The portfolio includes an automated bootstrap script:

```bash
cd ~/portfolio
chmod +x setup-agent.sh
./setup-agent.sh
```

This script:
- Verifies Node.js & installs pnpm@10.28.2
- Runs `pnpm install`
- Creates `.venv` and installs boomtick-cli (`td` CLI)
- Installs Playwright browsers with system dependencies
- Primes `.agent-context.json` via `pnpm run agent:prime`

### B. Bootstrap boomtick

```bash
cd ~/boomtick
# Python venv for td CLI & automation
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

# Node dependencies & MCP contracts build
pnpm install
pnpm --filter @arii/boomtick-mcp run build
```

---

## 3. Antigravity Environment Directory Structure

Antigravity uses specific directories for global configs, local settings, binaries, and lazy MCP schema discovery:

```
~/.gemini/
├── config/
│   ├── mcp_config.json                 # Global MCP server declarations
│   └── rules/                          # Global agent rules
│       └── browser_mcp_troubleshooting.md
└── antigravity-cli/
    ├── bin/                            # Custom MCP launcher scripts & wrappers
    │   ├── start_browsermcp.sh         # Browser MCP launcher
    │   └── boomtick-mcp -> ...         # Symlink to boomtick-mcp binary
    ├── mcp/                            # Lazy MCP schema discovery directory
    │   ├── boomtick-mcp -> ~/boomtick/mcp/.mcp/schemas
    │   ├── browser-mcp/                # Directory containing browser.json & instructions.md
    │   └── browsermcp -> browser-mcp   # Symlink alias
    ├── mcp_config.json                 # CLI MCP server definitions
    └── settings.json                   # Host permissions & server declarations
```

Create the required directories:

```bash
mkdir -p ~/.gemini/config/rules
mkdir -p ~/.gemini/antigravity-cli/bin
mkdir -p ~/.gemini/antigravity-cli/mcp/browser-mcp
```

---

## 4. Configuring Boomtick MCP (boomtick-mcp)

Boomtick provides PR rescue, merge conflict resolution, CI log inspection, and git workflow automation.

The canonical setup is scripted (idempotent — safe to re-run):

```bash
pnpm run setup:boomtick-mcp
# or: bash scripts/setup-boomtick-mcp.sh
# with overrides: GITHUB_REPO=tech-dancer BOOMTICK_REPO_PATH=/srv/repos/tech-dancer bash scripts/setup-boomtick-mcp.sh
```

It links the live schemas, resolves the server binary (global install
preferred, checkout build as fallback), and writes both
`mcp_config.json` files. `GITHUB_TOKEN` is read from the environment
when set and never written otherwise. The manual steps below remain as
fallback. If Antigravity already lists a working `boomtick` plugin,
skip this — a second `boomtick-mcp` server entry would only duplicate
its tools.

> **Important — Avoid Static Caches:** Never leave a static directory named `mcp/boomtick`. Always use a live symlink named `boomtick-mcp` pointing directly to `~/boomtick/mcp/.mcp/schemas`. This ensures tool schemas automatically update when contracts in boomtick change without schema drift or duplicate server registrations.

### A. Symlink the Live Schemas

```bash
ln -sfn ~/boomtick/mcp/.mcp/schemas ~/.gemini/antigravity-cli/mcp/boomtick-mcp
```

### B. Binary Symlink

```bash
ln -sfn ~/boomtick/mcp/dist/index.js ~/.gemini/antigravity-cli/bin/boomtick-mcp
# Or if installed globally:
# ln -sfn $(which boomtick-mcp) ~/.gemini/antigravity-cli/bin/boomtick-mcp
```

### C. Configure mcp_config.json

Add the server entry to both `~/.gemini/config/mcp_config.json` and `~/.gemini/antigravity-cli/mcp_config.json`:

```json
{
  "mcpServers": {
    "boomtick-mcp": {
      "command": "/home/<USER>/.gemini/antigravity-cli/bin/boomtick-mcp",
      "args": [],
      "env": {
        "BOOMTICK_REPO_PATH": "/home/<USER>/boomtick",
        "GITHUB_OWNER": "arii",
        "GITHUB_REPO": "boomtick",
        "GITHUB_TOKEN": "<YOUR_GITHUB_PERSONAL_ACCESS_TOKEN>",
        "PATH": "/home/<USER>/boomtick/.venv/bin:/home/<USER>/.local/bin:/usr/local/bin:/usr/bin:/bin"
      }
    }
  }
}
```

### D. Automated Host Setup via td

Alternatively, use the td CLI:

```bash
cd ~/boomtick
source .venv/bin/activate
td mcp install --host antigravity
```

---

## 5. Configuring Browser MCP (browser-mcp)

Browser MCP connects Antigravity to the user's active Google Chrome instance, allowing web inspection, form interaction, screenshot capture, and navigation.

### A. Create the Launcher Script

Create `~/.gemini/antigravity-cli/bin/start_browsermcp.sh`:

```bash
#!/bin/bash
exec npx -y @browsermcp/mcp@2.4.0 "$@"
```

> Pinned (#524): keep this version identical to the `browser-mcp` entry in
> `terminal/pi-config/mcp.json`. To bump, check
> https://github.com/BrowserMCP/mcp/releases, test locally, then update both
> pins in one commit.

Make it executable:

```bash
chmod +x ~/.gemini/antigravity-cli/bin/start_browsermcp.sh
```

### B. Declare in mcp_config.json

Add `browser-mcp` and the `browsermcp` alias in `~/.gemini/config/mcp_config.json`:

```json
{
  "mcpServers": {
    "browser-mcp": {
      "command": "/home/<USER>/.gemini/antigravity-cli/bin/start_browsermcp.sh"
    },
    "browsermcp": {
      "command": "/home/<USER>/.gemini/antigravity-cli/bin/start_browsermcp.sh"
    }
  }
}
```

### C. Create Schema and Symlink Alias

Create `~/.gemini/antigravity-cli/mcp/browser-mcp/browser.json`:

```json
{
  "name": "browser",
  "description": "Controls the user's Chrome. Runs one command per line, stopping at the first error. Actions print one ✓ line, reads print their output. Do many steps per call.\ngoto <url> | back | forward | reload\nclick <target>\nfill <target> \"<text>\" [--submit]\ntype \"<text>\"\npress <key>\nselect <target> \"<value>\"\ncheck <target> | uncheck <target>\nwait <ms> | wait --text \"<text>\" | wait <target>\ntab list | tab new [<url>] | tab select <id> | tab close\nsnapshot [<target>] [-i] [--depth N]\nfind \"<text>\"\nget text|value|count|url|title [<target>]\nscreenshot [<target>]\neval \"<js function>\"\nMore (help <command>): scroll, hover, drag, upload, dialog, console, requests, request\nTargets: e12 (a snapshot ref), getByRole('button', { name: 'Go' }), getByLabel/getByText/getByPlaceholder('…'), or CSS.\nQuote text with spaces: \"like this\".",
  "parameters": {
    "properties": {
      "commands": {
        "description": "Command lines, one per line",
        "type": "string"
      }
    },
    "required": [
      "commands"
    ],
    "type": "object"
  }
}
```

Create `~/.gemini/antigravity-cli/mcp/browser-mcp/instructions.md`:

```markdown
Browser MCP controls the user's Chrome through the `browser` tool:
1. Act in batches: put every step you can predict in one call (targets like getByLabel('Email') work without a snapshot).
2. Look with `snapshot -i` or `find "<text>"`; read a whole page with `get text --max all`.
3. Re-snapshot after navigating: refs from an older page fail.
```

Create the symlink alias:

```bash
ln -sfn ~/.gemini/antigravity-cli/mcp/browser-mcp ~/.gemini/antigravity-cli/mcp/browsermcp
```

### D. Chrome Extension Installation & Pairing

1. Open Google Chrome.
2. Install the **Browser MCP** extension from the Chrome Web Store.
3. Open `chrome://extensions/` ➔ **Browser MCP** ➔ **Details**:
   - Ensure **Site access** is set to **"On all sites"**.
4. The extension communicates with the local MCP server over a local WebSocket on port `9009`.

---

## 6. How to Use Browser MCP Best

Browser MCP v2.3.0 uses a single unified browser batch command interface rather than multiple fine-grained MCP tools.

### Best Practices

- **Batch Execution (Crucial for Speed):**
  Avoid making separate tool calls for each click or keystroke. Combine predictable actions into one multi-line commands string:
  ```
  goto https://example.com/login
  wait getByLabel('Email')
  fill getByLabel('Email') "user@example.com"
  fill getByLabel('Password') "secret" --submit
  ```

- **Use `snapshot -i` for Compact Inspection:**
  - `snapshot` dumps the full accessibility tree, which can quickly consume context window space.
  - Use `snapshot -i` to inspect interactive elements only (inputs, buttons, links).
  - Use `find "<text>"` to search for specific text on the page without dumping the tree.

- **Selector Priorities:**
  1. Semantic queries (work without prior snapshots):
     - `getByRole('button', { name: 'Submit' })`
     - `getByLabel('Username')`
     - `getByPlaceholder('Search...')`
     - `getByText('Continue')`
  2. Snapshot references (`e1`, `e12`):
     - Fast and unambiguous when referencing an immediately preceding snapshot.
     - *Warning:* References are invalidated upon navigation or page reload. Always re-snapshot after navigating!
  3. CSS / XPath: Use as a fallback when semantic locators or refs are ambiguous.

- **Iframe & Complex Dialog Workarounds:**
  If an embedded iframe or dialog doesn't expose its DOM hierarchy:
  - Use keyboard emulation:
    ```
    press Tab
    press ArrowDown
    press Enter
    ```
  - Capture visual state with `screenshot`.

- **Uploading Files:**
  ```
  upload getByLabel('Upload File') /home/user/Downloads/document.pdf
  ```

### Quick Command Reference

| Action | Command Syntax |
|---|---|
| Navigation | `goto <url>`, `back`, `forward`, `reload` |
| Click / Focus | `click <target>`, `hover <target>`, `scroll` |
| Typing | `fill <target> "<text>" [--submit]`, `type "<text>"` |
| Keys | `press Enter`, `press Tab`, `press Escape` |
| Dropdown / Check | `select <target> "<value>"`, `check <target>`, `uncheck <target>` |
| Waiting | `wait 2000`, `wait --text "Success"`, `wait <target>` |
| Tabs | `tab list`, `tab new [<url>]`, `tab select <id>`, `tab close` |
| Inspection | `snapshot [-i]`, `find "<text>"`, `get text <target>`, `screenshot` |
| JavaScript | `eval "() => document.title"` |

---

## 7. Troubleshooting Browser MCP

| Problem | Root Cause | Solution |
|---|---|---|
| "No connection to browser extension" | Extension not paired or server restarted. | Click the extension icon in Chrome toolbar. Click Disconnect then Connect to re-handshake. |
| Cannot attach to tab | Tab is a restricted browser internal page. | Extensions cannot connect to `chrome://*`, `edge://*`, or blank "New Tab". Navigate to any standard HTTP/HTTPS page first. |
| Multi-profile mismatch | Extension connected to a different profile. | Close other Chrome profile windows or ensure extension is active in the current profile. |
| Port 9009 unavailable | Stale node process holding port. | Check with `ss -tulpn \| grep 9009`. Kill stale process with `fuser -k 9009/tcp`. |
| Snapshot refs fail (`e12` not found) | Page navigated or re-rendered. | Re-run `snapshot -i` on the current page to acquire fresh element IDs. |

---

## 8. Antigravity Tool Hierarchy & Standards

When working in this ecosystem, agents and developers must adhere to the three-tier tool hierarchy defined in `AGENTS.md`:

1. **Tier 1 (MCP Protocol — Primary Interface):**
   - Connect via `boomtick-mcp` and `browser-mcp`.
   - Strongly-typed, contract-validated tools for PR triage, git branches, test execution, and browser automation.

2. **Tier 2 (CLI Entrypoint — `td`):**
   - `td` is the unified binary entrypoint:
     - `td doctor`: Full-stack runtime and diagnostic checks.
     - `td pr detect-conflicts`: Pull request conflict inspection.
     - `td audit`: Code quality and dependency audit.

3. **Tier 3 (Raw Shell / Git Operations — Fallback Only):**
   - Bash/git commands should only be used when no higher-tier MCP or td command exists.

---

## 9. Verification Checklist

Run these commands to verify the new computer configuration:

```bash
# 1. Verify Node and pnpm
node -v    # v24.x
pnpm -v    # 10.28.2

# 2. Verify td CLI diagnostics
td doctor

# 3. Verify Boomtick MCP server starts cleanly
/home/$USER/.gemini/antigravity-cli/bin/boomtick-mcp --help

# 4. Verify Browser MCP server version
/home/$USER/.gemini/antigravity-cli/bin/start_browsermcp.sh --version
# Expected output: 2.3.0

# 5. Verify portfolio test and media build
cd ~/portfolio
pnpm run verify
pnpm run build:media
```
