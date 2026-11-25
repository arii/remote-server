---
name: talker-browser
description: Drive authenticated browser web interfaces (ChatGPT, Claude, Mistral Vibe Code) via BrowserMCP for code review, adversarial testing, snippet prototyping, and copy simplification.
---

# Talker Browser

Drive authenticated web sessions via BrowserMCP for alternative architectural proposals, deep code reviews, adversarial edge-case reviews, rapid snippet prototyping, and copy/UX simplification.

## Targets & Navigation

### 1. Claude (https://claude.ai)
- Deep code review, architectural critique, and frontend UX audits.
- URL: `https://claude.ai/new` (or existing Claude chat tab).
- Locate tab via BrowserMCP `tab list`.
- Target input: `textbox "Write your prompt to Claude"`.
- Dispatch: `fill <ref> "<prompt>"` followed by `press Enter`.
- Completion signal: Latest message rendered under `feed "Chat messages"` with action toolbar (`Copy`, `Read aloud`, etc.).
- Extraction: Extract the completion from the newest `article` in `feed "Chat messages"`.

### 2. ChatGPT (https://chatgpt.com)
- Adversarial edge-case reviews, alternative algorithms, and copy simplification.
- URL: `https://chatgpt.com/` (or existing ChatGPT chat tab).
- Locate tab via BrowserMCP `tab list`.
- Target input: `textbox "Ask ChatGPT"`.
- Dispatch: `fill <ref> "<prompt>"` followed by `press Enter`.
- Completion signal: The `button "Stop"` disappears and the response paragraph under `heading "ChatGPT said:"` stabilizes.
- Extraction: Extract the content from the latest response container in `region "Conversation"`.

### 3. Mistral Vibe Code (https://chat.mistral.ai/code)
- Rapid snippet prototyping, web sandbox execution, and isolated code experiments.
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
