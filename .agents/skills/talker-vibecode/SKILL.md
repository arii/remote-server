---
name: talker-vibecode
description: Drive Mistral Vibe Code (https://chat.mistral.ai/code) via BrowserMCP for rapid snippet prototyping, web sandbox execution, and code generation.
---

# Talker Vibe Code

Drive the authenticated Mistral Vibe Code web interface via BrowserMCP to execute self-contained code experiments and obtain rapid snippet implementations.

## Tab & Navigation
- URL: `https://chat.mistral.ai/code` (or existing active code session tab).
- Locate tab via BrowserMCP `tab list`.
- Target input: Contenteditable prompt area under `What do you want to build?`.
- Dispatch: `click <ref>`, `type "<prompt>"`, `press Enter`.
- Completion signal: "Working / Preparing environment" pill transitions to `Worked for Xs` and the code block/response paragraph renders.
- Extraction: Extract output text and code blocks from `region "Conversation messages"`.

## Invariant
Always conclude every browser operation by selecting the primary terminal tab:
```
tab select 733028336
```
Never leave any browser tab active other than Zellij terminal.
