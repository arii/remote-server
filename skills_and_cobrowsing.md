# Deep Agent Skills & Co-Browsing — End-to-End Integration Guide
**Target Baseline:** `boomtick @ 37524e910081` | `remote-server @ 9bb46c4ab102`

---

## 1. Architectural Overview & Execution Sequence

Following the consolidation of `boomtick` (deterministic `td` engine + MCP server) and `remote-server` (LangGraph 4-stage pipeline), this guide provides the exact file-by-file implementation steps to integrate **Dynamic Progressive-Disclosure Skills** and **Managed Shared-Head Co-Browsing** across both repositories.

### System Data & Control Flow
```
┌────────────────────────────────────────────────────────────────────────────┐
│                        HUMAN OPERATOR (Web Dashboard)                      │
│  https://<domain>/buttons  ──►  Co-Browsing Drawer (<iframe src="/browser">)│
│       │ pointerdown / keydown                     │ [Resume Agent]         │
│       ▼                                           ▼                        │
│  POST /api/agent/pause                     POST /api/agent/resume          │
└───────┼───────────────────────────────────────────┼────────────────────────┘
        │                                           │
        ▼                                           ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                     remote-server (Cognitive Orchestrator)                 │
│                                                                            │
│  1. SkillsRegistry (deep-agent/skills_registry.py)                         │
│     ├─ Indexes YAML frontmatter (~100 tokens/skill) at startup             │
│     └─ Dynamically injects full Markdown runbooks per active LangGraph node│
│        • draft_spec_node           ◄── write_spec_issue.md                 │
│        • validated_replanner_node  ◄── coder_root_cause_analysis.md        │
│        • deep_semantic_auditor_node◄── architectural_audit_rubric.md       │
│        • td_impact_and_visual_node ◄── co_browsing_state_takeover.md       │
│                                                                            │
│  2. Pause/Resume State Ledger (backend/api/routers/agent_control.py)       │
│     └─ Halts CDP mutations on PAUSED_BY_USER; forces DOM re-snapshot on    │
│        resume before next LangGraph step.                                  │
└───────┼────────────────────────────────────────────────────────────────────┘
        │ Executes deterministic gates via `td <subcommand> --json`
        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                   boomtick (Deterministic Engine & CDP Driver)             │
│                                                                            │
│  • repo.read_agent_context (MCP) ──► Returns AGENT_CONTRACT + Skills Index │
│  • td audit-impact --json        ──► AST Blast Radius + Visual/DOM Diff    │
│  • browser_cdp.py                ──► Connects over CDP (no local Chrome!)  │
└───────┼────────────────────────────────────────────────────────────────────┘
        │ ws://browserless:3000 (Docker internal) or ws://127.0.0.1:3000 (Tailscale)
        ▼
┌────────────────────────────────────────────────────────────────────────────┐
│               browserless/chromium (Shared Browser Head Container)         │
│  • shm_size: 2gb | DEMO_MODE=true | PREBOOT_CHROME=true                    │
│  • Routed via Caddy `/browser*` behind GitHub OAuth (`oauth2-proxy`)       │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Phase 0: Pre-Flight Cleanup (Before Starting Issues 8–12)

Before wiring the new Skills and Co-Browsing layers, complete these two quick cleanups from the codebase audit so they do not conflict with the new modules:

1. **Unify Skills Discovery Across MCP & Deep-Agent:**
   - In `boomtick/mcp/src/tools/`, fold `skill.list.ts` and `skill.read.ts` into `repo.read_agent_context.ts` so external MCP clients receive the skill index in their initial bootstrap call, while internal LangGraph nodes use `deep-agent/skills_registry.py`.
2. **Delete Legacy `remote-server/deep-agent/ux_audit.py` and `impact.py`:**
   - Ensure `AuditorGraph` (`deep-agent/auditor.py`) exclusively calls `td audit-impact --worktree <path> --json` rather than importing legacy local Python scripts.

---

## 3. Phase 1: Containerized Shared Browser Head (Issue 8 — `remote-server`)

### 3.1 Add `browserless` to `remote-server/docker-compose.yml`
Add the persistent Chromium service with `2gb` shared memory (to prevent Chromium `SIGBUS` crashes on heavy DOM snapshots) and bind port `3000` to localhost/Tailscale while exposing it internally on the Compose network:

```yaml
  browserless:
    image: ghcr.io/browserless/chromium:latest
    container_name: browserless
    shm_size: "2gb"
    restart: unless-stopped
    expose:
      - "3000"
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      - DEMO_MODE=true
      - PREBOOT_CHROME=true
      - MAX_CONCURRENT_SESSIONS=5
      - CONNECTION_TIMEOUT=300000
      - TOKEN=${BROWSERLESS_TOKEN:-}
```

### 3.2 Wire `/browser*` Reverse Proxy in `remote-server/caddy/Caddyfile`
Route `/browser*` through Caddy (`:8080`, which already sits behind `oauth2-proxy` for human GitHub OAuth) and strip the `/browser` prefix so the Browserless debugger UI loads cleanly inside an iframe:

```caddy
:8080 {
    handle /buttons* {
        reverse_proxy buttons:8000
    }

    handle /browser* {
        uri strip_prefix /browser
        header -X-Frame-Options
        reverse_proxy browserless:3000 {
            transport http {
                dial_timeout 10s
            }
        }
    }

    handle {
        reverse_proxy terminal:7681
    }
}
```
*Note: Removing `X-Frame-Options` on `/browser*` ensures the same-origin `/buttons` dashboard can embed `/browser/` inside an `<iframe>` without browser security blocks.*

---

## 4. Phase 2: Remote CDP Driver & Visual Diff Engine (Issue 9 — `boomtick`)

Currently, `boomtick/cli/dev_tools/cli.py` (`cmd_audit_impact` at line 732) computes static Python/TS import dependents via `dependency_graph.py` and static regex checks via `ux_report.py`, with zero browser rendering or DOM diffing.

### 4.1 Create `boomtick/cli/dev_tools/services/browser_cdp.py`
Implement a pure remote-CDP client using Playwright's `connect_over_cdp` (never launching local Chromium binaries) and strictly routing all logs to `sys.stderr`:

```python
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class VisualDiffResult:
    reachable: bool
    url: str
    mismatch_percentage: float
    dom_node_count: int
    interactive_elements: int
    screenshot_path: str | None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _log_stderr(msg: str) -> None:
    """Guarantees stdout remains 100% pure JSON when --json is passed."""
    print(f"[browser_cdp] {msg}", file=sys.stderr)


def get_cdp_endpoint() -> str:
    endpoint = os.environ.get("BROWSERLESS_ENDPOINT", "ws://127.0.0.1:3000")
    token = os.environ.get("BROWSERLESS_TOKEN", "").strip()
    if token and "token=" not in endpoint:
        sep = "&" if "?" in endpoint else "?"
        endpoint = f"{endpoint}{sep}token={token}"
    return endpoint


def capture_dom_and_visual_snapshot(
    target_url: str,
    output_dir: Path,
    baseline_png: Path | None = None,
    viewport: dict[str, int] | None = None,
) -> VisualDiffResult:
    """Connects to remote browserless container over CDP, extracts DOM metrics & screenshot."""
    endpoint = get_cdp_endpoint()
    vp = viewport or {"width": 1280, "height": 800}
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_png = output_dir / "candidate_viewport.png"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        _log_stderr(f"Playwright not installed: {exc}")
        return VisualDiffResult(
            reachable=False,
            url=target_url,
            mismatch_percentage=0.0,
            dom_node_count=0,
            interactive_elements=0,
            screenshot_path=None,
            error="PLAYWRIGHT_NOT_INSTALLED",
        )

    try:
        with sync_playwright() as p:
            _log_stderr(f"Connecting over CDP to {endpoint.split('?')[0]}...")
            browser = p.chromium.connect_over_cdp(endpoint, timeout=10000)
            context = browser.contexts[0] if browser.contexts else browser.new_context(viewport=vp)
            page = context.new_page()
            page.set_viewport_size(vp)
            page.goto(target_url, wait_until="networkidle", timeout=15000)

            dom_metrics = page.evaluate(
                """() => ({
                    dom_node_count: document.querySelectorAll('*').length,
                    interactive_elements: document.querySelectorAll('a, button, input, select, textarea, [role="button"]').length
                })"""
            )
            page.screenshot(path=str(candidate_png), full_page=False)
            page.close()
            browser.close()

        mismatch = _compare_png_bytes(baseline_png, candidate_png) if baseline_png and baseline_png.exists() else 0.0
        return VisualDiffResult(
            reachable=True,
            url=target_url,
            mismatch_percentage=mismatch,
            dom_node_count=int(dom_metrics.get("dom_node_count", 0)),
            interactive_elements=int(dom_metrics.get("interactive_elements", 0)),
            screenshot_path=str(candidate_png),
        )
    except Exception as exc:
        _log_stderr(f"CDP snapshot failed: {exc}")
        return VisualDiffResult(
            reachable=False,
            url=target_url,
            mismatch_percentage=0.0,
            dom_node_count=0,
            interactive_elements=0,
            screenshot_path=None,
            error=f"BROWSERLESS_UNREACHABLE: {exc}",
        )


def _compare_png_bytes(baseline: Path, candidate: Path) -> float:
    """Lightweight pixel/byte comparison fallback if PIL/pixelmatch is absent."""
    b_bytes = baseline.read_bytes()
    c_bytes = candidate.read_bytes()
    if b_bytes == c_bytes:
        return 0.0
    try:
        from PIL import Image, ImageChops
        with Image.open(baseline).convert("RGB") as img1, Image.open(candidate).convert("RGB") as img2:
            if img1.size != img2.size:
                img2 = img2.resize(img1.size)
            diff = ImageChops.difference(img1, img2)
            non_zero = sum(1 for px in diff.getdata() if px != (0, 0, 0))
            total = img1.size[0] * img1.size[1]
            return round((non_zero / max(total, 1)) * 100.0, 3)
    except Exception:
        return 100.0
```

### 4.2 Wire `browser_cdp.py` into `cmd_audit_impact` (`boomtick/cli/dev_tools/cli.py:732`)
In `boomtick/cli/dev_tools/cli.py`, update `cmd_audit_impact` so that whenever `changed_files` touches UI extensions (`.tsx`, `.jsx`, `.vue`, `.html`, `.css`) and `--preview-url` (or `PREVIEW_URL` env var) is set, it attaches `visual_diff` to the JSON output:

```python
# Inside cmd_audit_impact in boomtick/cli/dev_tools/cli.py:
ui_extensions = {".tsx", ".jsx", ".vue", ".html", ".css", ".scss"}
ui_changed = any(Path(f).suffix in ui_extensions for f in changed_files)
visual_diff = None

preview_url = getattr(args, "preview_url", None) or os.environ.get("PREVIEW_URL")
if ui_changed and preview_url:
    from dev_tools.services.browser_cdp import capture_dom_and_visual_snapshot
    snap = capture_dom_and_visual_snapshot(
        target_url=preview_url,
        output_dir=worktree / ".td_rag" / "snapshots",
    )
    visual_diff = snap.to_dict()
    if not snap.reachable:
        passed = False
```

---

## 5. Phase 3: Dynamic Skills Registry & Progressive Disclosure (Issue 10 — `remote-server`)

Currently, `remote-server/deep-agent/skills.py` is a static Python dictionary and is not wired into `issue_generator.py`, `coder.py`, or `auditor.py`.

### 5.1 Create `remote-server/deep-agent/skills_registry.py`
Replace the static dictionary with a YAML-frontmatter progressive disclosure loader:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


class SkillNotFoundError(KeyError):
    """Raised when a requested skill markdown file does not exist."""


@dataclass(frozen=True)
class SkillMetadata:
    name: str
    description: str
    trigger_nodes: tuple[str, ...]
    path: Path

    def to_index_line(self) -> str:
        nodes = ", ".join(self.trigger_nodes) if self.trigger_nodes else "manual"
        return f"- **{self.name}** (`{nodes}`): {self.description}"


class SkillsRegistry:
    """Progressive disclosure skills registry for LangGraph nodes."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self.skills_dir = skills_dir or (Path(__file__).resolve().parent / "skills")
        self._meta_cache: dict[str, SkillMetadata] = {}
        self._body_cache: dict[str, str] = {}
        self.refresh_index()

    def refresh_index(self) -> None:
        self._meta_cache.clear()
        self._body_cache.clear()
        if not self.skills_dir.exists():
            return
        for md_path in sorted(self.skills_dir.glob("*.md")):
            meta, body = self._parse_skill_file(md_path)
            self._meta_cache[meta.name] = meta
            self._body_cache[meta.name] = body

    @staticmethod
    def _parse_skill_file(path: Path) -> tuple[SkillMetadata, str]:
        raw = path.read_text(encoding="utf-8")
        frontmatter: dict[str, Any] = {}
        body = raw
        if raw.startswith("---\n"):
            parts = raw.split("---\n", 2)
            if len(parts) == 3:
                frontmatter = yaml.safe_load(parts[1]) or {}
                body = parts[2].strip()
        name = str(frontmatter.get("name") or path.stem)
        description = str(frontmatter.get("description") or "").strip()
        triggers = tuple(str(t) for t in (frontmatter.get("trigger_nodes") or []))
        return SkillMetadata(name=name, description=description, trigger_nodes=triggers, path=path), body

    def get_progressive_index_summary(self) -> str:
        """Returns compact ~100-token index of all registered skills."""
        if not self._meta_cache:
            return "No dynamic skills registered."
        lines = ["### Available Domain Skills (Progressive Disclosure Index)"]
        for meta in self._meta_cache.values():
            lines.append(meta.to_index_line())
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        if name not in self._body_cache:
            raise SkillNotFoundError(f"Skill '{name}' not found in {self.skills_dir}")
        return self._body_cache[name]

    def inject_skills_for_node(self, node_name: str, base_system_prompt: str) -> str:
        """Appends full Markdown instructions ONLY for skills targeting `node_name`."""
        matched = [
            self._body_cache[name]
            for name, meta in self._meta_cache.items()
            if node_name in meta.trigger_nodes
        ]
        if not matched:
            return base_system_prompt
        injected_block = "\n\n---\n\n".join(matched)
        return f"{base_system_prompt.rstrip()}\n\n## Active Node Skills (`{node_name}`)\n\n{injected_block}\n"
```

### 5.2 Create the 4 Skill Markdown Files in `remote-server/deep-agent/skills/`
Create the 4 verbatim Markdown files specified in Issue 10:
1. `remote-server/deep-agent/skills/write_spec_issue.md` (`trigger_nodes: [draft_spec_node]`)
2. `remote-server/deep-agent/skills/coder_root_cause_analysis.md` (`trigger_nodes: [validated_replanner_node]`)
3. `remote-server/deep-agent/skills/architectural_audit_rubric.md` (`trigger_nodes: [deep_semantic_auditor_node]`)
4. `remote-server/deep-agent/skills/co_browsing_state_takeover.md` (`trigger_nodes: [visual_interaction_node, td_impact_and_visual_node]`)

### 5.3 Wire `SkillsRegistry` Into Each LangGraph Stage
1. **`remote-server/deep-agent/issue_generator.py` (`draft_spec_node`):**
   ```python
   from skills_registry import SkillsRegistry
   _SKILLS = SkillsRegistry()

   # Inside draft_spec_node:
   system_prompt = _SKILLS.inject_skills_for_node("draft_spec_node", BASE_SPEC_SYSTEM_PROMPT)
   ```
2. **`remote-server/deep-agent/coder.py` (`validated_replanner_node`):**
   ```python
   # Inside validated_replanner_node:
   system_prompt = _SKILLS.inject_skills_for_node("validated_replanner_node", BASE_REPLANNER_PROMPT)
   ```
3. **`remote-server/deep-agent/auditor.py` (`td_impact_and_visual_node` & `deep_semantic_auditor_node`):**
   ```python
   # Inside deep_semantic_auditor_node:
   system_prompt = _SKILLS.inject_skills_for_node("deep_semantic_auditor_node", BASE_AUDITOR_PROMPT)
   ```

---

## 6. Phase 4: Co-Browsing Takeover UI & Pause/Resume Synchronization (Issue 11 — `remote-server`)

### 6.1 Create Agent Control Router (`remote-server/backend/api/routers/agent_control.py`)
Provide atomic state transitions (`RUNNING` $\leftrightarrow$ `PAUSED_BY_USER`) backed by a shared JSON state file (`.agent_control_state.json`) so both FastAPI (`buttons`/`backend`) and CLI worker processes (`deep-agent/pipeline.py`) read and write the exact same state lock:

```python
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent", tags=["agent_control"])

STATE_FILE = Path(os.environ.get("AGENT_CONTROL_STATE_FILE", "/tmp/agent_control_state.json"))


class AgentControlState(BaseModel):
    state: Literal["RUNNING", "PAUSED_BY_USER", "IDLE"] = "IDLE"
    active_issue: int | None = None
    current_step: str = ""
    requires_dom_refresh: bool = False
    updated_at: float = 0.0


def read_control_state() -> AgentControlState:
    if not STATE_FILE.exists():
        return AgentControlState(updated_at=time.time())
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return AgentControlState(**data)
    except Exception:
        return AgentControlState(updated_at=time.time())


def write_control_state(state: AgentControlState) -> AgentControlState:
    state.updated_at = time.time()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
    return state


@router.get("/state")
def get_agent_state() -> dict[str, Any]:
    return read_control_state().model_dump()


@router.post("/pause")
def pause_agent(reason: str = "user_takeover") -> dict[str, Any]:
    current = read_control_state()
    current.state = "PAUSED_BY_USER"
    current.current_step = f"Paused ({reason})"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()


@router.post("/resume")
def resume_agent() -> dict[str, Any]:
    current = read_control_state()
    current.state = "RUNNING"
    current.current_step = "Resuming — mandatory DOM re-snapshot scheduled"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()
```
Register `agent_control.router` in `remote-server/backend/api/main.py` (and proxy `/api/agent/*` in `buttons/server.py` if `buttons` serves the UI directly).

### 6.2 Embed Co-Browsing Takeover Drawer (`remote-server/buttons/static/co_browsing.js`)
Attach immediate takeover hooks so any human `pointerdown` or `keydown` on the browser canvas pauses the agent before a race condition can occur:

```javascript
(function () {
  let agentState = "IDLE";
  const statusBanner = document.getElementById("cobrowse-status-banner");
  const resumeBtn = document.getElementById("cobrowse-resume-btn");
  const overlay = document.getElementById("cobrowse-takeover-interceptor");

  async function syncState() {
    try {
      const res = await fetch("/api/agent/state");
      const data = await res.json();
      agentState = data.state;
      renderState(data);
    } catch (err) {
      console.error("Failed to fetch agent control state:", err);
    }
  }

  function renderState(data) {
    if (!statusBanner || !resumeBtn || !overlay) return;
    if (data.state === "PAUSED_BY_USER") {
      statusBanner.textContent = "Manual Control Active — Agent Paused";
      statusBanner.className = "cobrowse-banner paused";
      resumeBtn.style.display = "inline-block";
      overlay.style.pointerEvents = "none"; // Allow full interaction with /browser/ iframe
    } else if (data.state === "RUNNING") {
      statusBanner.textContent = `Agent Active (${data.current_step || "running"}) — Click canvas to take over`;
      statusBanner.className = "cobrowse-banner running";
      resumeBtn.style.display = "none";
      overlay.style.pointerEvents = "auto"; // Intercept first click to trigger instant pause
    } else {
      statusBanner.textContent = "Agent Idle — Shared Browser Ready";
      statusBanner.className = "cobrowse-banner idle";
      resumeBtn.style.display = "none";
      overlay.style.pointerEvents = "none";
    }
  }

  async function triggerImmediateTakeover() {
    if (agentState !== "RUNNING") return;
    overlay.style.pointerEvents = "none";
    statusBanner.textContent = "Pausing Agent — Taking Over Control...";
    await fetch("/api/agent/pause", { method: "POST" });
    await syncState();
  }

  async function resumeAgent() {
    statusBanner.textContent = "Synchronizing DOM & Resuming Agent...";
    await fetch("/api/agent/resume", { method: "POST" });
    await syncState();
  }

  if (overlay) {
    overlay.addEventListener("pointerdown", triggerImmediateTakeover, { passive: true });
    overlay.addEventListener("keydown", triggerImmediateTakeover, { passive: true });
  }
  if (resumeBtn) {
    resumeBtn.addEventListener("click", resumeAgent);
  }

  setInterval(syncState, 2000);
  syncState();
})();
```
*Architectural Note on Cross-Iframe Events:* Because browser security prevents parent windows from listening to `pointerdown` inside a cross-document `<iframe>` unless injected, placing a transparent `#cobrowse-takeover-interceptor` div (`pointer-events: auto` **only** while `agentState === "RUNNING"`) over the iframe captures the user's very first click, fires `POST /api/agent/pause` in $<5\text{ ms}$, and immediately sets `pointer-events: none` so the user interacts seamlessly with the underlying `/browser/` debugger.

### 6.3 Enforce Pause Check & Mandatory Re-Snapshot in `deep-agent/pipeline.py`
In `remote-server/deep-agent/pipeline.py`, wrap browser-interacting and gate steps with `await_if_paused_and_refresh_dom()`:

```python
def await_if_paused_and_refresh_dom(worktree_path: Path, preview_url: str | None = None) -> dict[str, Any] | None:
    """Blocks while PAUSED_BY_USER; forces a fresh DOM snapshot via td audit-impact upon resume."""
    from backend.api.routers.agent_control import read_control_state, write_control_state

    state = read_control_state()
    while state.state == "PAUSED_BY_USER":
        time.sleep(0.5)
        state = read_control_state()

    if state.requires_dom_refresh:
        state.requires_dom_refresh = False
        state.current_step = "Re-indexing DOM state post-takeover"
        write_control_state(state)
        if preview_url:
            return core.run_td(
                ["audit-impact", "--worktree", str(worktree_path), "--preview-url", preview_url],
                cwd=worktree_path,
            )
    return None
```

---

## 7. Phase 5: End-to-End Verification & `agy` Preflight Suite (Issue 12)

Run the following verification commands across both repositories once Phases 1–4 are implemented:

```bash
# 1. Verify boomtick Remote CDP Driver & Visual Audit Unit Tests
cd boomtick
pytest cli/tests/test_browser_cdp.py cli/tests/test_visual_audit.py

# 2. Verify remote-server SkillsRegistry & Co-Browsing Control API
cd ../remote-server
pytest tests/test_skills_registry.py tests/test_co_browsing_api.py

# 3. Run End-to-End Co-Browsing Preflight (validates Browserless pressure, CDP snapshot, Pause/Resume lock, and td audit-impact visual_diff)
python3 scripts/verify-co-browsing-preflight.py
pytest tests/test_co_browsing_e2e.py
```
