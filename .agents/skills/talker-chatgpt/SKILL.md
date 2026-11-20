---
name: talker-chatgpt
description: Drive ChatGPT web interface (https://chatgpt.com) via BrowserMCP for adversarial testing, alternative algorithms, and copy simplification.
---

# Talker ChatGPT

Drive the authenticated ChatGPT web session via BrowserMCP for alternative architectural proposals, adversarial edge-case reviews, and copy/UX simplification.

## Tab & Navigation
- URL: `https://chatgpt.com/` (or existing ChatGPT chat tab).
- Locate tab via BrowserMCP `tab list`.
- Target input: `textbox "Ask ChatGPT"`.
- Dispatch: `fill <ref> "<prompt>"` followed by `press Enter`.
- Completion signal: The `button "Stop"` disappears and the response paragraph under `heading "ChatGPT said:"` stabilizes.
- Extraction: Extract the content from the latest response container in `region "Conversation"`.

## Invariant
Always conclude every browser operation by selecting the primary terminal tab:
```
tab select 733028336
```
Never leave any browser tab active other than Zellij terminal.
