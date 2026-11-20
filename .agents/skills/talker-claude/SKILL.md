---
name: talker-claude
description: Drive Claude web interface (https://claude.ai) via BrowserMCP for deep code review, architectural critique, and frontend UX audits.
---

# Talker Claude

Drive the authenticated Claude web session via BrowserMCP to obtain high-precision code reviews, refactoring plans, and frontend UX evaluations.

## Tab & Navigation
- URL: `https://claude.ai/new` (or existing Claude chat tab).
- Locate tab via BrowserMCP `tab list`.
- Target input: `textbox "Write your prompt to Claude"`.
- Dispatch: `fill <ref> "<prompt>"` followed by `press Enter`.
- Completion signal: Latest message rendered under `feed "Chat messages"` with action toolbar (`Copy`, `Read aloud`, etc.).
- Extraction: Extract the completion from the newest `article` in `feed "Chat messages"`.

## Invariant
Always conclude every browser operation by selecting the primary terminal tab:
```
tab select 733028336
```
Never leave any browser tab active other than Zellij terminal.
