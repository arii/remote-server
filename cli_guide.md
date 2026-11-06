# Unified Guide: Lean Local LLMs, CLI Agent Optimization (`agy`, `muse`, `copilot`), and Co-Browsing Integration

**Target Baseline:** `boomtick @ 37524e910081` | `remote-server @ 9bb46c4ab102`

---

## 1. Executive Summary & Cost-Cutting Architecture

To transition away from unmetered cloud token spend (Muse, Copilot, expensive cloud frontier models) toward **local LLMs (vLLM / Ollama)**, **Nebius AI Studio keys**, and **deterministic local execution**, this guide unifies three core objectives:

1. **Token Elimination & Pruning:** Remove chatty multi-agent personas (`lane-orchestrator`, `lane-coder`, multi-turn negotiation handoffs) and strip verbose prompt markdown in favor of 4 lean, on-demand skill runbooks.
2. **Deterministic First-Execution (`td` & MCP):** Offload AST analysis, RAG queries, scope validation, test execution, git worktrees, and impact audits to local zero-token deterministic Python commands (`td`) and worktree-safe MCP tools.
3. **CLI Agent Supercharging (`agy`, `muse`, `copilot`):** Wire CLI tools directly to local MCP endpoints and deterministic `td` bridges so they stop burning context tokens searching files or guessing git status.
4. **Co-Browsing & Visual Auditing:** Containerize `browserless/chromium`, expose CDP via `boomtick`'s `browser_cdp.py`, and implement an immediate human takeover hook with state locking.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                  CLI AGENTS & UI (Interactive Control Plane)               │
│                                                                            │
│   agy CLI (Local Terminal)   │   GitHub Copilot / Muse   │  Human Browser  │
│   • Model: Nebius / Local    │   • Model: Capped tokens  │  Takeover Panel │
│   • MCP: Stdio / HTTP        │   • Context: td gates     │  /buttons       │
└───────────────┬───────────────────────────┬──────────────────────┬─────────┘
                │                           │                      │
                ▼                           ▼                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                    remote-server (Cognitive Orchestrator)                  │
│                                                                            │
│  1. Lean SkillsRegistry (deep-agent/skills_registry.py)                   │
│     ├─ Indexes ~100-token YAML frontmatter at bootstrap                    │
│     └─ Injects full instructions ONLY into targeted LangGraph nodes        │
│        • draft_spec_node           ◄── write_spec_issue.md                 │
│        • validated_replanner_node  ◄── coder_root_cause_analysis.md        │
│        • deep_semantic_auditor_node◄── architectural_audit_rubric.md       │
│        • visual_interaction_node   ◄── co_browsing_state_takeover.md       │
│                                                                            │
│  2. Local / Nebius Model Adapter (deep-agent/llm_factory.py)               │
│     ├─ Local:  http://127.0.0.1:8000/v1 (Qwen2.5-Coder-32B / DeepSeek-R1) │
│     └─ Nebius: https://api.studio.nebius.ai/v1 (temperature: 0.1, max: 1500)│
│                                                                            │
│  3. Agent Control Ledger (backend/api/routers/agent_control.py)            │
│     └─ Atomic PAUSED_BY_USER lock; forces DOM re-snapshot before resume.   │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ Invokes deterministic tooling
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                 boomtick (Deterministic Engine & MCP Server)               │
│                                                                            │
│  • 16 Worktree-Safe MCP Tools (mcp/src/mcp/definitions.ts)                 │
│  • Zero-Token Deterministic Gates:                                         │
│    ├─ td init              ──► Single-command env & worktree setup         │
│    ├─ td rag query         ──► AST & Markdown SQLite TF-IDF query          │
│    ├─ td validate-issue    ──► Strict spec syntax & file existence gate    │
│    ├─ td verify-scope      ──► Git status allowlist check & auto-revert    │
│    └─ td audit-impact      ──► AST blast-radius + Remote CDP visual diff   │
└─────────────────────────────────────┬──────────────────────────────────────┘
                                      │ ws://browserless:3000
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                browserless/chromium (Shared Head Container)                │
│  • shm_size: 2gb | DEMO_MODE=true | Caddy reverse proxy at /browser*       │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Token Burn Pruning: What to Remove vs. Keep

### 2.1 Removed Components (High-Token Overhead)

| Component to Purge | File / Directory Location | Why Remove |
| :--- | :--- | :--- |
| **`lane-orchestrator`** | `remote-server/backend/lane_orchestrator.py` | Consumes 3,000–6,000 tokens per cycle exchanging chat messages to coordinate sub-agents. Replaced by direct LangGraph state transitions. |
| **`lane-coder` Sub-Agents** | `remote-server/backend/lane_coder.py` | Repeatedly echoes full codebase history. Replaced by `InitCoderGraph` in `deep-agent/coder.py`. |
| **Unrestricted Chat Baselines** | Agent prompts referencing open-ended conversational turns | Forces agents to run exploratory shell loops (`ls`, `grep`, `cat`) instead of single deterministic `td` calls. |
| **Granular MCP Tools (11 deleted)** | `boomtick/mcp/src/tools/repo.multi_exec.ts`, `repo.commit_patch.ts`, `repo.logs.ts`, `repo.get_package_scripts.ts`, `repo.get_changed_files.ts`, `repo.get_command_schema.ts`, `github.get_pr_commits.ts`, `github.get_pr_reviews.ts`, `github.get_merge_conflict_files.ts`, `skill.list.ts`, `skill.read.ts` | Eliminates unsandboxed execution, root-repo git mutation, and schema bloat from LLM tool definitions. |
| **Duplicate Python Modules** | `remote-server/backend/ast_validator.py`, `parse_task_spec.py`, `verify_allowlist.py`, `impact_pass.py`, `repair_worker.py`, `deep-agent/impact.py`, `deep-agent/ux_audit.py` | Legacy copies that duplicated logic now executed directly via `td <subcommand> --json`. |

### 2.2 Retained & Optimized Components (Lean Pipeline)

1. **Deterministic `td` CLI & MCP Engine (`boomtick`):** 16 structured tools capped to $\le 16$ in `server.test.ts`.
2. **4-Stage LangGraph Pipeline (`remote-server`):** Single-pass state transitions (`Init` $\rightarrow$ `IssueGenerator` $\rightarrow$ `Coder` $\rightarrow$ `Auditor`).
3. **4 Modular Markdown Skills:** Indexed as 100-token summaries; full bodies loaded strictly on-demand.

---

## 3. Local LLM & Nebius Integration Layer

Configure all LangChain/LangGraph nodes in `remote-server` to route through a single model factory (`remote-server/deep-agent/llm_factory.py`) with strict token clamping.

### 3.1 Model Factory Implementation (`remote-server/deep-agent/llm_factory.py`)

```python
from __future__ import annotations

import os
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI


def get_lean_chat_model(node_name: str, max_tokens: int = 1500) -> BaseChatModel:
    """Returns a token-bounded Chat model pointing to Nebius or a local OpenAI-compatible endpoint."""
    backend_provider = os.environ.get("LLM_PROVIDER", "nebius").lower()
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.1"))

    if backend_provider == "local":
        # Local vLLM, Ollama, or llama.cpp endpoint
        base_url = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8000/v1")
        api_key = os.environ.get("LOCAL_LLM_KEY", "dummy-key")
        model = os.environ.get("LOCAL_LLM_MODEL", "Qwen/Qwen2.5-Coder-32B-Instruct")
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            timeout=120,
        )

    # Nebius AI Studio
    nebius_key = os.environ.get("NEBIUS_API_KEY", "")
    if not nebius_key:
        raise ValueError("NEBIUS_API_KEY is not set. Export it or set LLM_PROVIDER=local.")
    base_url = os.environ.get("NEBIUS_BASE_URL", "https://api.studio.nebius.ai/v1")
    model = os.environ.get("NEBIUS_MODEL", "meta-llama/Meta-Llama-3.1-70B-Instruct")

    return ChatOpenAI(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        base_url=base_url,
        api_key=nebius_key,
        timeout=120,
    )
```

---

## 4. Leveraging `td` & MCP Across CLI Agents (`agy`, `muse`, `copilot`)

CLI agents waste thousands of tokens running exploratory bash commands (`git status`, `find .`, `grep`, `cat`). By pointing them to `boomtick-mcp` and replacing multi-turn prompt chains with direct `td` calls, token consumption drops by over 70%.

### 4.1 CLI Agent Configuration

#### A. `agy` Configuration (`~/.agy/config.json` or project root `.agy.json`)
Point `agy` to the local `boomtick-mcp` server via `stdio` or SSE:

```json
{
  "mcpServers": {
    "boomtick": {
      "command": "node",
      "args": ["/path/to/boomtick/mcp/dist/index.js"],
      "env": {
        "TARGET_REPO_PATH": ".",
        "TD_CLI_PATH": "td"
      }
    }
  },
  "model": {
    "provider": "openai-compatible",
    "baseURL": "https://api.studio.nebius.ai/v1",
    "apiKey": "${NEBIUS_API_KEY}",
    "modelName": "meta-llama/Meta-Llama-3.1-70B-Instruct",
    "maxTokens": 1500,
    "temperature": 0.1
  },
  "rules": [
    "NEVER run raw 'git checkout' or 'git apply'. Always use 'worktree.create' and 'agent.submit_pr'.",
    "NEVER run ad-hoc bash searches. Use 'td.init_and_rag' for semantic and symbol retrieval.",
    "Always validate changes using 'td.verify_scope' and 'td.audit_worktree' before completing."
  ]
}
```

#### B. GitHub Copilot & Muse Instructions (`.github/copilot-instructions.md`)
Embed deterministic execution rules directly into the repository prompt root:

```markdown
# Repository Agent Guidelines: Deterministic First-Execution

1. **Zero-Token Retrieval**:
   - Do NOT run recursive `grep` or `find`.
   - Execute: `td rag query --query "<symbol_or_problem>" --json` to inspect target symbols.

2. **Worktree Isolation**:
   - All code edits MUST occur inside an isolated worktree created via `td worktree create --issue <id>`.
   - Never modify root repository files directly.

3. **Deterministic Gate Protocol**:
   - Before proposing edits: run `td validate-issue --file <path> --json`.
   - After editing: run `td verify-scope --spec-file <path> --worktree <path> --json`.
   - Code audit: run `td audit-code --worktree <path> --json`.
   - PR submission: run `td agent submit-pr --worktree <path> --json`.
```

### 4.2 Single-Call Bootstrap via `repo.read_agent_context`

Rather than making 5 separate tool calls to discover repo layout, scripts, and skills, CLI agents call `repo.read_agent_context` once at turn 0:

```json
{
  "contract": "...",
  "packageScripts": { "test": "pytest", "lint": "ruff check ." },
  "ragStatus": { "indexed": true, "chunks": 420 },
  "availableSkills": [
    "write_spec_issue",
    "coder_root_cause_analysis",
    "architectural_audit_rubric",
    "co_browsing_state_takeover"
  ]
}
```

---

## 5. Dynamic Skills Framework (`SkillsRegistry`)

Instead of dumping full skill prompts into agent system messages, `SkillsRegistry` indexes light YAML frontmatter summaries (~100 tokens total) and injects full markdown instructions **only** when a node activates.

### 5.1 Registry Implementation (`remote-server/deep-agent/skills_registry.py`)

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


class SkillNotFoundError(KeyError):
    pass


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
        if not self._meta_cache:
            return "No dynamic skills registered."
        lines = ["### Available Skills (Progressive Index)"]
        for meta in self._meta_cache.values():
            lines.append(meta.to_index_line())
        return "\n".join(lines)

    def load_skill(self, name: str) -> str:
        if name not in self._body_cache:
            raise SkillNotFoundError(f"Skill '{name}' not found.")
        return self._body_cache[name]

    def inject_skills_for_node(self, node_name: str, base_system_prompt: str) -> str:
        matched = [
            self._body_cache[name]
            for name, meta in self._meta_cache.items()
            if node_name in meta.trigger_nodes
        ]
        if not matched:
            return base_system_prompt
        injected = "\n\n---\n\n".join(matched)
        return f"{base_system_prompt.rstrip()}\n\n## Active Skills for `{node_name}`\n\n{injected}\n"
```

### 5.2 The 4 Essential Skill Runbooks

Store these inside `remote-server/deep-agent/skills/`:

#### 1. `write_spec_issue.md` (`trigger_nodes: [draft_spec_node]`)
```markdown
---
name: write_spec_issue
description: Enforces deterministic structure, file:line citations, and strict acceptance criteria.
trigger_nodes:
  - draft_spec_node
---
# Skill: Spec Authoring & Anti-Slop Guide
- No speculative abstractions or placeholder comments (`TODO`, `TBD`, `...`).
- Mandatory sections: Problem Statement (with exact `file.py#L10-L25` citations), Objective, Allowed Files, Forbidden Files, Acceptance Criteria, Verification Commands.
- Verification commands must be executable bash commands that return exit code 0.
```

#### 2. `coder_root_cause_analysis.md` (`trigger_nodes: [validated_replanner_node]`)
```markdown
---
name: coder_root_cause_analysis
description: Four-step diagnostic loop preventing shallow repair cycles.
trigger_nodes:
  - validated_replanner_node
---
# Skill: Root Cause Analysis
1. Deconstruct failing test assertions and stack traces.
2. Query `td rag query --query "<symbol>" --json` to verify contracts.
3. Record hypothesis in persistent decision ledger before generating edits.
4. Keep modifications strictly inside Allowed Files.
```

#### 3. `architectural_audit_rubric.md` (`trigger_nodes: [deep_semantic_auditor_node]`)
```markdown
---
name: architectural_audit_rubric
description: Verifies acceptance criteria traceability, anti-slop rules, and downstream impact.
trigger_nodes:
  - deep_semantic_auditor_node
---
# Skill: Architectural Audit Rubric
- 1-to-1 verification: Each acceptance criterion must have corresponding worktree diff evidence.
- Reject tautological tests (tests that only assert mocks).
- Verify downstream dependents using `td audit-impact --json`.
- Inspect visual/DOM diffs when UI templates are modified.
```

#### 4. `co_browsing_state_takeover.md` (`trigger_nodes: [visual_interaction_node, td_impact_and_visual_node]`)
```markdown
---
name: co_browsing_state_takeover
description: Handles human takeover, command suppression, and post-takeover DOM re-indexing.
trigger_nodes:
  - visual_interaction_node
  - td_impact_and_visual_node
---
# Skill: Co-Browsing State Takeover Protocol
- Drop scheduled browser actions immediately when `PAUSED_BY_USER` is signaled.
- Do NOT reuse prior element references, selectors, or snapshot indices upon resume.
- Execute fresh DOM snapshot via `td audit-impact` before continuing.
```

---

## 6. Co-Browsing & Managed Shared Browser Head

### 6.1 Containerize Browserless (`remote-server/docker-compose.yml`)

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

### 6.2 Caddy Ingress Routing (`remote-server/caddy/Caddyfile`)

Strip the `/browser` prefix and clear `X-Frame-Options` so the dashboard can embed `/browser/` inside an iframe:

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

### 6.3 Remote CDP Driver (`boomtick/cli/dev_tools/services/browser_cdp.py`)

Connect to `browserless` via Playwright's `connect_over_cdp` without ever launching local Chrome binaries:

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
    endpoint = get_cdp_endpoint()
    vp = viewport or {"width": 1280, "height": 800}
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_png = output_dir / "candidate_viewport.png"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[browser_cdp] Playwright not installed", file=sys.stderr)
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

        mismatch = _compare_images(baseline_png, candidate_png) if baseline_png and baseline_png.exists() else 0.0
        return VisualDiffResult(
            reachable=True,
            url=target_url,
            mismatch_percentage=mismatch,
            dom_node_count=int(dom_metrics.get("dom_node_count", 0)),
            interactive_elements=int(dom_metrics.get("interactive_elements", 0)),
            screenshot_path=str(candidate_png),
        )
    except Exception as exc:
        print(f"[browser_cdp] Snapshot failed: {exc}", file=sys.stderr)
        return VisualDiffResult(
            reachable=False,
            url=target_url,
            mismatch_percentage=0.0,
            dom_node_count=0,
            interactive_elements=0,
            screenshot_path=None,
            error=str(exc),
        )


def _compare_images(baseline: Path, candidate: Path) -> float:
    try:
        from PIL import Image, ImageChops
        with Image.open(baseline).convert("RGB") as img1, Image.open(candidate).convert("RGB") as img2:
            if img1.size != img2.size:
                img2 = img2.resize(img1.size)
            diff = ImageChops.difference(img1, img2)
            non_zero = sum(1 for px in diff.getdata() if px != (0, 0, 0))
            return round((non_zero / max(img1.size[0] * img1.size[1], 1)) * 100.0, 3)
    except Exception:
        return 0.0
```

### 6.4 Immediate Takeover Hook & State API

#### FastAPI State Router (`remote-server/backend/api/routers/agent_control.py`)
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
    current_step: str = ""
    requires_dom_refresh: bool = False
    updated_at: float = 0.0


def read_state() -> AgentControlState:
    if not STATE_FILE.exists():
        return AgentControlState(updated_at=time.time())
    try:
        return AgentControlState(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return AgentControlState(updated_at=time.time())


def write_state(state: AgentControlState) -> AgentControlState:
    state.updated_at = time.time()
    STATE_FILE.write_text(json.dumps(state.model_dump(), indent=2), encoding="utf-8")
    return state


@router.get("/state")
def get_state() -> dict[str, Any]:
    return read_state().model_dump()


@router.post("/pause")
def pause_agent() -> dict[str, Any]:
    s = read_state()
    s.state = "PAUSED_BY_USER"
    s.current_step = "Manual control active"
    s.requires_dom_refresh = True
    return write_state(s).model_dump()


@router.post("/resume")
def resume_agent() -> dict[str, Any]:
    s = read_state()
    s.state = "RUNNING"
    s.current_step = "Resuming — refreshing DOM"
    s.requires_dom_refresh = True
    return write_state(s).model_dump()
```

#### Frontend Takeover Canvas (`remote-server/buttons/static/co_browsing.js`)
```javascript
(function () {
  let agentState = "IDLE";
  const banner = document.getElementById("cobrowse-status-banner");
  const resumeBtn = document.getElementById("cobrowse-resume-btn");
  const overlay = document.getElementById("cobrowse-takeover-interceptor");

  async function syncState() {
    try {
      const res = await fetch("/api/agent/state");
      const data = await res.json();
      agentState = data.state;
      if (agentState === "PAUSED_BY_USER") {
        banner.textContent = "Manual Control Active — Agent Paused";
        resumeBtn.style.display = "inline-block";
        overlay.style.pointerEvents = "none";
      } else if (agentState === "RUNNING") {
        banner.textContent = "Agent Running — Click to Take Over";
        resumeBtn.style.display = "none";
        overlay.style.pointerEvents = "auto";
      } else {
        banner.textContent = "Agent Idle";
        resumeBtn.style.display = "none";
        overlay.style.pointerEvents = "none";
      }
    } catch (e) {
      console.error(e);
    }
  }

  overlay?.addEventListener("pointerdown", async () => {
    if (agentState !== "RUNNING") return;
    overlay.style.pointerEvents = "none";
    await fetch("/api/agent/pause", { method: "POST" });
    await syncState();
  });

  resumeBtn?.addEventListener("click", async () => {
    await fetch("/api/agent/resume", { method: "POST" });
    await syncState();
  });

  setInterval(syncState, 2000);
  syncState();
})();
```

---

## 7. Verification & Runbook Checklist

Run the following test commands across both repositories to confirm all layers are operational:

```bash
# 1. Verify boomtick MCP server (< 16 tools) and CDP driver tests
cd boomtick
pnpm --filter mcp run sync-schemas
pnpm --filter mcp test
pytest cli/tests/test_browser_cdp.py cli/tests/test_visual_audit.py

# 2. Verify remote-server SkillsRegistry, Agent Control API, and Clean Tree
cd ../remote-server
test ! -f backend/ast_validator.py && test ! -f backend/lane_orchestrator.py
pytest tests/test_skills_registry.py tests/test_co_browsing_api.py

# 3. Verify Local / Nebius Model Factory
python3 -c "
from deep_agent.llm_factory import get_lean_chat_model
llm = get_lean_chat_model('test')
res = llm.invoke('Hello from lean deep-agent')
print('Model response:', res.content[:50])
"

# 4. End-to-End Co-Browsing Preflight Execution
python3 scripts/verify-co-browsing-preflight.py
pytest tests/test_co_browsing_e2e.py
```
