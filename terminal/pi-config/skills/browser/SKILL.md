---
name: browser
description: Instructions and command reference for calling Browser MCP to control Chrome, inspect UI elements, capture screenshots, and automate web interactions.
---

# Browser MCP Tools & Interaction Reference

Browser MCP v2.3.0 controls the active Chrome instance through a single unified `browser` tool rather than individual micro-tools.

## Core Interaction Discipline

1. **Batch Execution (Crucial for Speed):**
   Combine predictable actions into one multi-line `commands` string instead of separate roundtrips:
   ```
   goto https://example.com/login
   wait getByLabel('Email')
   fill getByLabel('Email') "user@example.com"
   fill getByLabel('Password') "secret" --submit
   ```

2. **Compact Inspection with `snapshot -i`:**
   - Plain `snapshot` outputs the full accessibility tree, quickly bloating context windows.
   - Use `snapshot -i` to inspect only interactive elements (inputs, links, buttons).
   - Use `find "<text>"` to locate text snippets on page without dumping the tree.

3. **Selector Priorities:**
   - **Semantic queries** (run without prior snapshots):
     - `getByRole('button', { name: 'Submit' })`
     - `getByLabel('Username')`
     - `getByPlaceholder('Search...')`
     - `getByText('Continue')`
   - **Snapshot references** (`e1`, `e12`):
     - Exact references from the preceding snapshot.
     - *Important:* References invalidate upon page navigation or reload. Always re-run `snapshot -i` after navigating.
   - **CSS / XPath**: Fallback for ambiguous elements.

4. **Keyboard Emulation & Iframe Workarounds:**
   - For non-standard popups or unexposed iframes:
     ```
     press Tab
     press ArrowDown
     press Enter
     ```
   - Capture visual state with `screenshot`.

## Quick Command Reference

- **Navigation:** `goto <url>`, `back`, `forward`, `reload`
- **Click / Focus:** `click <target>`, `hover <target>`, `scroll`
- **Typing:** `fill <target> "<text>" [--submit]`, `type "<text>"`
- **Keys:** `press Enter`, `press Tab`, `press Escape`
- **Selection:** `select <target> "<value>"`, `check <target>`, `uncheck <target>`
- **Waiting:** `wait 2000`, `wait --text "Success"`, `wait <target>`
- **Tabs:** `tab list`, `tab new [<url>]`, `tab select <id>`, `tab close`
- **Inspection:** `snapshot -i`, `find "<text>"`, `get text <target>`, `screenshot`
- **JavaScript:** `eval "() => document.title"`
- **File Upload:** `upload <target> <file_path>`
