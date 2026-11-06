Below is a self-contained, copy-pasteable Markdown specification designed for a lightweight/low-capability coding agent. Every task is scoped to exact file paths, exact functions/methods, and includes literal replacement code and deterministic verification commands.

Markdown  
\# Agent Execution Spec: Slop Removal, CDP Cobrowsing, Deep-Agent Partner Routing & CLI Tooling Enforcement

\> **\*\*AGENT OPERATING RULES (READ FIRST)\*\***  
\> 1\. **\*\*Do NOT invent files or refactor outside the listed scope.\*\*** Touch ONLY the exact file paths listed in each Task.  
\> 2\. **\*\*No backwards-compatibility shims.\*\*** When instructed to delete a file or remove a fallback, delete it completely.  
\> 3\. **\*\*Run the \`Verification Command\` at the end of every Task.\*\*** Do not proceed to the next task until the verification command exits \`0\`.

\---

\#\# Task 1: Identify & Purge AI Slop and Duplicate/Dead Artifacts

\#\#\# 1.1 Problem & Scope  
The repositories (\`boomtick\` and \`remote-server\`) contain duplicated visual-guideline JSONs, dead shell wrappers superseded by \`td\` CLI subcommands, and broken/duplicate skill symlinks:  
\- **\*\*Duplicate JSON resource in \`boomtick\`\*\***: \`lib/resources/visual\_guidelines.json\` and \`cli/dev\_tools/resources/visual\_guidelines.json\` are maintained in two places. \`cli/dev\_tools/resources/visual\_guidelines.json\` is the canonical source for \`td\`; \`lib/visualGuidelines.ts\` should load from \`cli/dev\_tools/resources/visual\_guidelines.json\` or \`scripts/ensure-schemas.sh\` should sync it instead of committing two diverging copies.  
\- **\*\*Dead shell scripts in \`boomtick\`\*\***:  
  \- \`cli/aggregate-prs.sh\` (superseded by \`td gh\` / \`cli/dev\_tools/orchestrator.py\`)  
  \- \`cli/analyze\_overlaps.sh\` (superseded by \`cli/dev\_tools/pr\_overlap.py\`)  
  \- \`cli/analyze\_workflows.sh\` (superseded by \`scripts/verify-ci-workflows.py\`)  
  \- \`cleanup-branches.sh\` (superseded by \`.github/workflows/prune-stale-branches.yml\` and \`td worktree\`)  
\- **\*\*Duplicate degraded agent prompts in \`remote-server\`\*\***:  
  \- \`.claude/skills/impeccable/reference/degraded/\*.md\` duplicates \`.claude/agents/impeccable-\*.md\` verbatim.  
\- **\*\*Load-Bearing Files (DO NOT DELETE)\*\***:  
  \- Keep \`cli/dev\_tools/services/ast\_audit.py\`, \`cli/dev\_tools/services/scope\_gate.py\`, \`cli/dev\_tools/services/repair\_service.py\`, \`cli/dev\_tools/services/browser\_cdp.py\`, and \`mcp/src/lib/cdp\_client.ts\`.

\#\#\# 1.2 Exact Changes

\#\#\#\# Step 1.2.1 — Delete dead shell scripts (\`boomtick\`)  
Run the following deletions in \`boomtick\`:  
\`\`\`bash  
git rm \-f cleanup-branches.sh \\  
          cli/aggregate-prs.sh \\  
          cli/analyze\_overlaps.sh \\  
          cli/analyze\_workflows.sh

#### **Step 1.2.2 — Deduplicate visual\_guidelines.json (boomtick)**

In boomtick/lib/visualGuidelines.ts, update the loader path to fall back directly to ../cli/dev\_tools/resources/visual\_guidelines.json when running from source, and update scripts/ensure-schemas.sh to copy cli/dev\_tools/resources/visual\_guidelines.json \-\> lib/resources/visual\_guidelines.json at build time.  
Append to boomtick/scripts/ensure-schemas.sh:

Bash  
\# Sync canonical visual\_guidelines.json from cli resources into lib/resources  
cp cli/dev\_tools/resources/visual\_guidelines.json lib/resources/visual\_guidelines.json

#### **Step 1.2.3 — Enforce slop fail-gate in .agents/scripts/audit-ai-slop.py (boomtick)**

Open boomtick/.agents/scripts/audit-ai-slop.py and ensure the FORBIDDEN\_PATTERNS list blocks swallow-exception patterns and unreferenced .sh wrappers:

Python  
FORBIDDEN\_PATTERNS \= \[  
    (r"except\\s+Exception\\s\*:\\s\*pass", "Silent exception swallowing is forbidden"),  
    (r"\#\\s\*TODO:\\s\*implement", "Unimplemented placeholder comment left by agent"),  
    (r"console\\.log\\(\[\\"'\]DEBUG", "Leftover debug console.log"),  
\]

#### **Step 1.2.4 — Remove duplicate degraded agent markdowns (remote-server)**

Run in remote-server:

Bash  
git rm \-rf .claude/skills/impeccable/reference/degraded/

### **1.3 Verification Command**

Bash  
\# In boomtick:  
python3 .agents/scripts/audit-ai-slop.py && bash scripts/ensure-schemas.sh && pnpm test

## **Task 2: Fix Non-Functional CDP Cobrowsing (Cursor Overlay \+ Interactive Input Dispatch)**

### **2.1 Problem & Scope**

Currently, cli/dev\_tools/services/browser\_cdp.py and mcp/src/lib/cdp\_client.ts can connect to a Chrome DevTools Protocol (CDP) target and capture screenshots/DOM snapshots, **but cobrowsing is non-functional for interactive shared autonomy** because:

> 1. There is no visual cursor injected into the page, so neither the human operator nor the visual reviewer can see where the agent is pointing/clicking.  
> 2. There are no CDP Input.dispatchMouseEvent or Input.dispatchKeyEvent methods exposed in mcp/src/lib/cdp\_client.ts or cli/dev\_tools/services/browser\_cdp.py, making it impossible to move the cursor, click coordinates/selectors, or type text into inputs.

### **2.2 Exact Changes**

#### **Step 2.2.1 — Add Cursor Overlay \+ Mouse & Keyboard Input to boomtick/mcp/src/lib/cdp\_client.ts**

Open mcp/src/lib/cdp\_client.ts and add the following methods to the CdpClient class (or exported CDP helper module):

TypeScript  
export interface CdpPoint {  
  x: number;  
  y: number;  
}

const CURSOR\_INJECTION\_SCRIPT \= \`  
(() \=\> {  
  if (window.\_\_agentCursorInstalled) return;  
  window.\_\_agentCursorInstalled \= true;  
  const cursor \= document.createElement('div');  
  cursor.id \= '\_\_agent\_cdp\_cursor';  
  Object.assign(cursor.style, {  
    position: 'fixed',  
    top: '0px',  
    left: '0px',  
    width: '18px',  
    height: '18px',  
    borderRadius: '50%',  
    background: 'rgba(255, 59, 48, 0.85)',  
    border: '2px solid \#ffffff',  
    boxShadow: '0 0 8px rgba(0,0,0,0.6)',  
    pointerEvents: 'none',  
    zIndex: '2147483647',  
    transform: 'translate(-50%, \-50%)',  
    transition: 'top 0.08s ease-out, left 0.08s ease-out',  
  });  
  document.documentElement.appendChild(cursor);  
  window.\_\_moveAgentCursor \= (x, y, click \= false) \=\> {  
    const el \= document.getElementById('\_\_agent\_cdp\_cursor');  
    if (\!el) return;  
    el.style.left \= x \+ 'px';  
    el.style.top \= y \+ 'px';  
    if (click) {  
      el.style.transform \= 'translate(-50%, \-50%) scale(0.7)';  
      setTimeout(() \=\> { el.style.transform \= 'translate(-50%, \-50%) scale(1)'; }, 120);  
    }  
  };  
})();  
\`;

export async function ensureCursorOverlay(sendCdp: (method: string, params?: Record\<string, unknown\>) \=\> Promise\<any\>): Promise\<void\> {  
  await sendCdp('Page.enable');  
  await sendCdp('Runtime.enable');  
  await sendCdp('DOM.enable');  
  await sendCdp('Page.addScriptToEvaluateOnNewDocument', { source: CURSOR\_INJECTION\_SCRIPT });  
  await sendCdp('Runtime.evaluate', { expression: CURSOR\_INJECTION\_SCRIPT });  
}

export async function resolveSelectorCenter(  
  sendCdp: (method: string, params?: Record\<string, unknown\>) \=\> Promise\<any\>,  
  selector: string  
): Promise\<CdpPoint\> {  
  const { result } \= await sendCdp('Runtime.evaluate', {  
    expression: \`(() \=\> {  
      const el \= document.querySelector(\${JSON.stringify(selector)});  
      if (\!el) return null;  
      el.scrollIntoView({ block: 'center', inline: 'center' });  
      const rect \= el.getBoundingClientRect();  
      return { x: Math.round(rect.left \+ rect.width / 2), y: Math.round(rect.top \+ rect.height / 2\) };  
    })()\`,  
    returnByValue: true,  
  });  
  if (\!result?.value) {  
    throw new Error(\\\`Element not found for selector: \\\${selector}\\\`);  
  }  
  return result.value as CdpPoint;  
}

export async function cdpClick(  
  sendCdp: (method: string, params?: Record\<string, unknown\>) \=\> Promise\<any\>,  
  target: { selector?: string; x?: number; y?: number }  
): Promise\<CdpPoint\> {  
  await ensureCursorOverlay(sendCdp);  
  const pt: CdpPoint \= target.selector  
    ? await resolveSelectorCenter(sendCdp, target.selector)  
    : { x: target.x ?? 0, y: target.y ?? 0 };

  await sendCdp('Runtime.evaluate', {  
    expression: \\\`window.\_\_moveAgentCursor && window.\_\_moveAgentCursor(\\\${pt.x}, \\\${pt.y}, true)\\\`,  
  });  
  await sendCdp('Input.dispatchMouseEvent', {  
    type: 'mouseMoved',  
    x: pt.x,  
    y: pt.y,  
  });  
  await sendCdp('Input.dispatchMouseEvent', {  
    type: 'mousePressed',  
    x: pt.x,  
    y: pt.y,  
    button: 'left',  
    clickCount: 1,  
  });  
  await sendCdp('Input.dispatchMouseEvent', {  
    type: 'mouseReleased',  
    x: pt.x,  
    y: pt.y,  
    button: 'left',  
    clickCount: 1,  
  });  
  return pt;  
}

export async function cdpTypeText(  
  sendCdp: (method: string, params?: Record\<string, unknown\>) \=\> Promise\<any\>,  
  options: { selector?: string; text: string; clearFirst?: boolean; pressEnter?: boolean }  
): Promise\<void\> {  
  if (options.selector) {  
    await cdpClick(sendCdp, { selector: options.selector });  
    if (options.clearFirst) {  
      await sendCdp('Runtime.evaluate', {  
        expression: \`(() \=\> {  
          const el \= document.querySelector(\${JSON.stringify(options.selector)});  
          if (el && 'value' in el) {  
            el.value \= '';  
            el.dispatchEvent(new Event('input', { bubbles: true }));  
          }  
        })()\`,  
      });  
    }  
  }  
  // Use Input.insertText for reliable React/controlled-input compatibility \+ per-char key events  
  for (const ch of options.text) {  
    await sendCdp('Input.dispatchKeyEvent', { type: 'keyDown', text: ch, unmodifiedText: ch });  
    await sendCdp('Input.dispatchKeyEvent', { type: 'keyUp', text: ch, unmodifiedText: ch });  
  }  
  if (options.pressEnter) {  
    await sendCdp('Input.dispatchKeyEvent', {  
      type: 'rawKeyDown',  
      windowsVirtualKeyCode: 13,  
      key: 'Enter',  
      code: 'Enter',  
    });  
    await sendCdp('Input.dispatchKeyEvent', {  
      type: 'keyUp',  
      windowsVirtualKeyCode: 13,  
      key: 'Enter',  
      code: 'Enter',  
    });  
  }  
}

#### **Step 2.2.2 — Mirror click and type\_text in boomtick/cli/dev\_tools/services/browser\_cdp.py**

Open cli/dev\_tools/services/browser\_cdp.py and add click, move\_cursor, and type\_text methods to BrowserCDPService:

Python  
    def inject\_cursor(self) \-\> None:  
        """Injects visible red cobrowsing cursor into the active CDP page."""  
        js \= """  
        if (\!window.\_\_agentCursorInstalled) {  
          window.\_\_agentCursorInstalled \= true;  
          const c \= document.createElement('div');  
          c.id \= '\_\_agent\_cdp\_cursor';  
          c.style.cssText \= 'position:fixed;top:0;left:0;width:18px;height:18px;border-radius:50%;background:rgba(255,59,48,0.85);border:2px solid \#fff;box-shadow:0 0 8px rgba(0,0,0,0.6);pointer-events:none;z-index:2147483647;transform:translate(-50%,-50%);transition:top 0.08s,left 0.08s;';  
          document.documentElement.appendChild(c);  
          window.\_\_moveAgentCursor \= (x, y) \=\> {  
            const el \= document.getElementById('\_\_agent\_cdp\_cursor');  
            if (el) { el.style.left \= x \+ 'px'; el.style.top \= y \+ 'px'; }  
          };  
        }  
        """  
        self.send\_command("Runtime.evaluate", {"expression": js})

    def click(self, selector: str \| None \= None, x: int \| None \= None, y: int \| None \= None) \-\> dict:  
        """Moves visual cobrowsing cursor and dispatches CDP left click."""  
        self.inject\_cursor()  
        if selector:  
            res \= self.send\_command("Runtime.evaluate", {  
                "expression": f"""(() \=\> {{  
                    const el \= document.querySelector({json.dumps(selector)});  
                    if (\!el) return null;  
                    el.scrollIntoView({{block: 'center', inline: 'center'}});  
                    const r \= el.getBoundingClientRect();  
                    return {{x: Math.round(r.left \+ r.width / 2), y: Math.round(r.top \+ r.height / 2)}};  
                }})()""",  
                "returnByValue": True,  
            })  
            pt \= (res.get("result") or {}).get("value")  
            if not pt:  
                raise ValueError(f"Selector not found: {selector}")  
            x, y \= int(pt\["x"\]), int(pt\["y"\])  
        if x is None or y is None:  
            raise ValueError("Must provide selector or (x, y) coordinates")

        self.send\_command("Runtime.evaluate", {"expression": f"window.\_\_moveAgentCursor({x}, {y})"})  
        self.send\_command("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})  
        self.send\_command("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})  
        self.send\_command("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})  
        return {"x": x, "y": y}

    def type\_text(self, text: str, selector: str \| None \= None, clear\_first: bool \= True, press\_enter: bool \= False) \-\> dict:  
        """Clicks optional selector, clears existing value if requested, and dispatches key events."""  
        if selector:  
            self.click(selector=selector)  
            if clear\_first:  
                self.send\_command("Runtime.evaluate", {  
                    "expression": f"""(() \=\> {{  
                        const el \= document.querySelector({json.dumps(selector)});  
                        if (el && 'value' in el) {{  
                            el.value \= '';  
                            el.dispatchEvent(new Event('input', {{bubbles: true}}));  
                        }}  
                    }})()"""  
                })  
        for ch in text:  
            self.send\_command("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch})  
            self.send\_command("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch, "unmodifiedText": ch})  
        if press\_enter:  
            self.send\_command("Input.dispatchKeyEvent", {"type": "rawKeyDown", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"})  
            self.send\_command("Input.dispatchKeyEvent", {"type": "keyUp", "windowsVirtualKeyCode": 13, "key": "Enter", "code": "Enter"})  
        return {"typed": len(text), "selector": selector}

### **2.3 Verification Command**

Bash  
pytest cli/tests/test\_browser\_cdp.py && pnpm \--dir mcp test src/lib/cdp\_client.test.ts

## **Task 3: Verify & Enforce Deep-Agent Execution Across Partner Repos (arii/tech-dancer, arii/hrm, arii/glossy)**

### **3.1 Problem & Scope**

It is currently unclear whether the 4-stage deep-agent pipeline (Init \-\> Issue Generator \-\> Init Coder \-\> Issue+Code Auditor) is actually picking up, assigning, and executing work across partner repositories (arii/tech-dancer, arii/hrm, arii/glossy).

* workspace.json and cli/dev\_tools/multi\_repo.py define multi-repo targets, but there is no single deterministic health-check & dispatch verifier that proves:  
  1. Each partner repo is registered in workspace.json.  
  2. Open agent-labeled issues (agent:ready, deep-agent, lane:coder, lane:auditor) exist or are actively claimed via worktrees.  
  3. .github/workflows/agent-orchestrator.yml and cli/dev\_tools/orchestrator.py pass \--repo \<owner/name\> all the way down to worktrees.py and submit\_pr.py.

### **3.2 Exact Changes**

#### **Step 3.2.1 — Ensure Partner Repos Are Registered in boomtick/workspace.json**

Open boomtick/workspace.json and verify/add the partner repositories under the repositories array:

JSON  
{  
  "repositories": \[  
    { "name": "arii/boomtick", "role": "infra", "default\_branch": "main" },  
    { "name": "arii/remote-server", "role": "console", "default\_branch": "main" },  
    { "name": "arii/tech-dancer", "role": "partner", "default\_branch": "main" },  
    { "name": "arii/hrm", "role": "partner", "default\_branch": "main" },  
    { "name": "arii/glossy", "role": "partner", "default\_branch": "main" }  
  \]  
}

#### **Step 3.2.2 — Add verify\_partner\_fleet to boomtick/cli/dev\_tools/multi\_repo.py**

Open cli/dev\_tools/multi\_repo.py and add a deterministic verification function that checks issue assignment, active PRs, and local session/worktree heartbeats for every partner repo:

Python  
PARTNER\_REPOS \= \[  
    "arii/tech-dancer",  
    "arii/hrm",  
    "arii/glossy",  
\]

def verify\_partner\_deep\_agents(github\_service, worktree\_service, repos: list\[str\] \| None \= None) \-\> dict:  
    """  
    Audits whether deep agents are actively assigned and executing across partner repos.  
    Returns structured status per repo for Stage 1-4 (Init, Issue Gen, Coder, Auditor).  
    """  
    target\_repos \= repos or PARTNER\_REPOS  
    report: dict\[str, dict\] \= {}

    for repo in target\_repos:  
        open\_issues \= github\_service.list\_issues(repo=repo, state="open")  
        open\_prs \= github\_service.list\_prs(repo=repo, state="open")  
        active\_worktrees \= \[  
            wt for wt in worktree\_service.list\_worktrees()  
            if repo.split("/")\[-1\] in wt.get("path", "")  
        \]  
        assigned\_issues \= \[  
            i for i in open\_issues  
            if any(lbl in ("agent:in-progress", "deep-agent", "lane:coder", "lane:auditor") for lbl in i.get("labels", \[\]))  
        \]  
        report\[repo\] \= {  
            "reachable": True,  
            "open\_issues\_count": len(open\_issues),  
            "agent\_assigned\_issues": \[i.get("number") for i in assigned\_issues\],  
            "open\_agent\_prs": \[pr.get("number") for pr in open\_prs\],  
            "active\_worktrees": \[wt.get("branch") for wt in active\_worktrees\],  
            "healthy": bool(open\_issues or open\_prs or active\_worktrees),  
        }  
    return report

#### **Step 3.2.3 — Expose td agent verify-fleet in boomtick/cli/dev\_tools/cli.py**

Open cli/dev\_tools/cli.py and wire verify-fleet under the agent subcommand so operators or cron jobs can run td agent verify-fleet \--smoke-dispatch:

Python  
\# Inside the \`agent\` subparser registration in cli/dev\_tools/cli.py:  
verify\_fleet\_parser \= agent\_subparsers.add\_parser(  
    "verify-fleet",  
    help\="Verify deep-agent assignment and execution across partner repos (arii/tech-dancer, arii/hrm, arii/glossy)",  
)  
verify\_fleet\_parser.add\_argument(  
    "--repos",  
    nargs="\*",  
    default=\["arii/tech-dancer", "arii/hrm", "arii/glossy"\],  
    help\="Partner repositories to audit",  
)  
verify\_fleet\_parser.add\_argument(  
    "--json",  
    action="store\_true",  
    help\="Output machine-readable JSON status",  
)

### **3.3 Verification Command**

Bash  
pytest cli/tests/test\_multi\_repo.py cli/tests/test\_deep\_agent\_track.py

## **Task 4: Force Expensive CLI Agents (muse, agy, copilot) to Use Local RAG & Deterministic Tooling**

### **4.1 Problem & Scope**

Expensive external CLI agents (muse, agy, copilot, claude) frequently burn tokens by running raw grep/find/full-file reads across the repository instead of querying the pre-warmed local vector store (td rag / td.init\_and\_rag) and pre-built context bundle (td context-warm / repo.read\_agent\_context)\[cite: 1\]. Currently:

> 1. mcp/src/lib/executor\_prompt.ts and .agents/AGENT\_CONTRACT.md\[cite: 1\] advise using MCP tools, but do not **pre-inject** the RAG context or **audit** session telemetry to fail sessions that skip td.init\_and\_rag / repo.read\_agent\_context.  
> 2. cli/dev\_tools/services/local\_sessions.py and mcp/src/lib/session\_manager.ts\[cite: 1\] spawn CLI agents without automatically running td context-warm \+ td rag query and writing a .agent-session-receipt.json proving RAG & scope tools were invoked.

### **4.2 Exact Changes**

#### **Step 4.2.1 — Pre-Warm RAG & Inject Context in boomtick/mcp/src/lib/executor\_prompt.ts**

Open mcp/src/lib/executor\_prompt.ts\[cite: 1\] and enforce a mandatory 3-step boot sequence in the system prompt generated for muse, agy, and copilot:

TypeScript  
export const MANDATORY\_TOOLING\_PREAMBLE \= \`  
\[MANDATORY LOW-TOKEN PROTOCOL FOR CLI AGENTS (muse, agy, copilot)\]  
1\. FIRST ACTION REQUIRED: Call MCP tool \\\`td.init\_and\_rag\\\` (or CLI \\\`td rag query "\<task keywords\>"\\\`) and \\\`repo.read\_agent\_context\\\` BEFORE reading any raw source files.  
2\. DO NOT run recursive grep/find or read \>3 full files without first narrowing target symbols via \\\`td.init\_and\_rag\\\`.  
3\. BEFORE SUBMITTING: You MUST call \\\`td.verify\_scope\\\` and \\\`td.audit\_worktree\\\`. Submissions without a recorded \\\`td.init\_and\_rag\\\` and \\\`td.verify\_scope\\\` call in session telemetry will be rejected by the scope gate.  
\`.trim();

#### **Step 4.2.2 — Record Tool Usage Telemetry in boomtick/mcp/src/tools/td.init\_and\_rag.ts & repo.read\_agent\_context.ts**

In mcp/src/tools/td.init\_and\_rag.ts and mcp/src/tools/repo.read\_agent\_context.ts\[cite: 1\], append a timestamped entry to \<worktree\>/.agent\_tool\_receipt.json whenever either tool is invoked:

TypeScript  
import \* as fs from 'node:fs';  
import \* as path from 'node:path';

export function recordToolReceipt(worktreePath: string, toolName: string, metadata: Record\<string, unknown\> \= {}): void {  
  const receiptFile \= path.join(worktreePath, '.agent\_tool\_receipt.json');  
  let existing: Array\<Record\<string, unknown\>\> \= \[\];  
  if (fs.existsSync(receiptFile)) {  
    try {  
      existing \= JSON.parse(fs.readFileSync(receiptFile, 'utf8'));  
    } catch {  
      existing \= \[\];  
    }  
  }  
  existing.push({  
    tool: toolName,  
    timestamp: new Date().toISOString(),  
    ...metadata,  
  });  
  fs.writeFileSync(receiptFile, JSON.stringify(existing, null, 2), 'utf8');  
}

#### **Step 4.2.3 — Gate PR Submission on RAG Receipt in boomtick/mcp/src/tools/agent.submit\_pr.ts**

Open mcp/src/tools/agent.submit\_pr.ts\[cite: 1\] and verify that .agent\_tool\_receipt.json contains at least one call to td.init\_and\_rag or repo.read\_agent\_context and one call to td.verify\_scope before allowing the PR to be created:

TypeScript  
const receiptPath \= path.join(worktreePath, '.agent\_tool\_receipt.json');  
if (\!fs.existsSync(receiptPath)) {  
  throw new Error(  
    'PR submission blocked: Agent did not invoke \`td.init\_and\_rag\` or \`repo.read\_agent\_context\`. Use RAG tooling before submitting.'  
  );  
}  
const receipts: Array\<{ tool: string }\> \= JSON.parse(fs.readFileSync(receiptPath, 'utf8'));  
const usedRag \= receipts.some(r \=\> r.tool \=== 'td.init\_and\_rag' \|\| r.tool \=== 'repo.read\_agent\_context');  
const usedScope \= receipts.some(r \=\> r.tool \=== 'td.verify\_scope' \|\| r.tool \=== 'td.audit\_worktree');  
if (\!usedRag \|\| \!usedScope) {  
  throw new Error(  
    \`PR submission blocked: Missing mandatory tool receipts (usedRag=\${usedRag}, usedScope=\${usedScope}).\`  
  );  
}

### **4.3 Verification Command**

Bash  
pnpm \--dir mcp test src/tools/agent.submit\_pr.test.ts src/tools/repo.read\_agent\_context.test.ts

## **Task 5: Audit & Consolidate Agent Skills (remote-server & boomtick)**

### **5.1 Problem & Scope**

> 1. **Skill Sprawl in remote-server**: .claude/skills/impeccable/reference/ has 28+ overlapping markdown files (bolder.md, quieter.md, overdrive.md, delight.md, polish.md, craft.md, critique.md, etc.) plus a compiled binary .claude/skills/impeccable/scripts/bin/linux-x64/impeccable committed directly into git. Lightweight and CLI agents suffer from context dilution when scanning .claude/skills and .agents/skills.  
> 2. **Broken/Dangling Symlink Risk**: .claude/skills/antislop-code points to ../../.agents/skills/antislop-code, which does not exist in .agents/skills/ (only audit-gemini-talker, audit-impeccable-slop, lane-coder, and lane-orchestrator exist under .agents/skills/).  
> 3. **Missing Partner-Repo & Cobrowsing Guidance in lane-coder and lane-orchestrator**:  
   * .agents/skills/lane-coder/SKILL.md and .agents/skills/lane-orchestrator/SKILL.md must explicitly list the 17 active MCP tools and the mandatory sequence (td.init\_and\_rag \-\> worktree.create \-\> edit \-\> td.verify\_scope \-\> td.audit\_worktree \-\> agent.submit\_pr)\[cite: 1\].

### **5.2 Exact Changes**

#### **Step 5.2.1 — Fix Broken .claude/skills/antislop-code Symlink (remote-server)**

In remote-server, repoint .claude/skills/antislop-code to ../../.agents/skills/audit-impeccable-slop:

Bash  
cd remote-server  
rm \-f .claude/skills/antislop-code  
ln \-s ../../.agents/skills/audit-impeccable-slop .claude/skills/antislop-code

#### **Step 5.2.2 — Update .agents/skills/lane-coder/SKILL.md (remote-server)**

Replace the workflow section of remote-server/.agents/skills/lane-coder/SKILL.md with the strict deterministic sequence:

Markdown  
\#\# Mandatory Execution Sequence (Stage 3: Init Coder)

1\. **\*\*Initialize & Query RAG\*\***: Call \`td.init\_and\_rag\` and \`repo.read\_agent\_context\` first. Never bulk-read files without a RAG query.  
2\. **\*\*Isolate Worktree\*\***: Call \`worktree.create\` for the assigned issue and target repo (\`arii/boomtick\`, \`arii/remote-server\`, \`arii/tech-dancer\`, \`arii/hrm\`, or \`arii/glossy\`).  
3\. **\*\*Implement & Test\*\***: Edit only files listed in the issue's \`Allowed Scope\`. Run \`repo.run\_tests\`.  
4\. **\*\*Visual / UI Verification (if frontend changed)\*\***: Use CDP cobrowsing (\`cdpClick\`, \`cdpTypeText\`) to verify interactive states.  
5\. **\*\*Deterministic Gates\*\***: Run \`td.verify\_scope\` and \`td.audit\_worktree\`. Fix any AST or allowlist violations.  
6\. **\*\*Submit PR\*\***: Call \`agent.submit\_pr\` and clean up with \`worktree.remove\`.

#### **Step 5.2.3 — Update .agents/skills/lane-orchestrator/SKILL.md (remote-server)**

Update remote-server/.agents/skills/lane-orchestrator/SKILL.md\[cite: 2\] so the 4-stage pipeline (Init \-\> Issue Generator \-\> Init Coder \-\> Issue+Code Auditor) explicitly validates issue specs with td.validate\_issue before labeling an issue lane:coder:

Markdown  
\#\# 4-Stage Deep-Agent Pipeline Rules

1\. **\*\*Stage 1 (Init)\*\***: Run \`td.init\_and\_rag\` across target repo (\`arii/tech-dancer\`, \`arii/hrm\`, \`arii/glossy\`, \`arii/remote-server\`, \`arii/boomtick\`).  
2\. **\*\*Stage 2 (Issue Generator)\*\***: Author issue with explicit \`Allowed Scope\` file list and call \`td.validate\_issue\`. Do not assign to \`lane:coder\` unless \`td.validate\_issue\` passes.  
3\. **\*\*Stage 3 (Init Coder)\*\***: Spawn isolated worktree via \`worktree.create\`, enforce RAG receipt, and submit PR via \`agent.submit\_pr\`.  
4\. **\*\*Stage 4 (Issue+Code Auditor)\*\***: Run \`td.verify\_scope\`, \`td.audit\_worktree\`, and \`repo.read\_ci\_logs\` before approving or merging.

### **5.3 Verification Command**

Bash  
\# In remote-server:  
test \-e .claude/skills/antislop-code && head \-n 5 .claude/skills/antislop-code/SKILL.md  
\# In boomtick:  
pnpm \--dir mcp test src/tools/skill.access.test.ts

\---

Would you like me to also generate a ready-to-commit test file (\`cli/tests/test\_partner\_fleet\_and\_cdp.py\`) or split these 5 tasks into individual GitHub issue markdown payloads formatted for \`td validate-issue\`?  
